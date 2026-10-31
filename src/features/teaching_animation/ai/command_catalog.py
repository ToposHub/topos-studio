"""Extended stable command vocabulary for Motion Graphic editing (MG-09).

The base :class:`~src.features.teaching_animation.ai.commands.TeachingAICommandRegistry`
only compiles four command names (``add_element`` / ``update_element`` /
``reorder_element`` / ``set_animation``) — enough for an initial clip but not
for the "把开场缩短到 2 秒、标题改为 X、卡片依次弹出、换成品牌蓝" edits MG-09
wants the post-generation editor to support.

This module extends that vocabulary with a second registry that compiles a
richer set of *typed, dry-run-safe* commands.  Every command:

* has a JSON schema fragment (so the LLM can be constrained to emit it);
* compiles to one or more existing :class:`~src.features.teaching_animation.core.commands.SceneCommand`
  instances — we do not invent new core commands here, we recombine the
  existing ones so undo/redo and the command stack keep working;
* is range-checked and complexity-bounded so a model cannot, e.g., ask for a
  100000-frame scene or 500 elements.

Design rules:

* closed vocabulary — unknown names raise, never silently no-op;
* every command name is reversible through the existing SceneCommandStack
  (each compiled SceneCommand carries its own undo);
* the extended registry is composable with the base registry — the host can
  dispatch a command to whichever registry recognizes it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from ..core.commands import (
    AddElementCommand,
    AddMarkerCommand,
    RemoveElementCommand,
    SceneCommand,
    SetDurationCommand,
    UpdateElementCommand,
)
from ..core.design_tokens import BrandTheme, read_scene_tokens
from ..core.model import SceneAnimation, SceneAssetRef, SceneElement, SceneMarker, SceneTransform, TeachingSceneDocument
from ..core.template_commands import (
    ApplyTemplateCommand,
    ReplaceAssetSlotCommand,
    SetBrandThemeCommand,
)
from ..motion.motion_commands import (
    ApplyMotionPresetCommand,
    StaggerElementsCommand,
)
from .commands import (
    TeachingAICommand,
    TeachingAICommandRegistry,
    TeachingAIProposal,
    TeachingAIProposalError,
    _hash_scene,
)
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack


def _coerce_interpolation(value: str) -> InterpolationType:
    """Accept either an enum or its value (e.g. ``"ease_in_out"``)."""
    if isinstance(value, InterpolationType):
        return value
    try:
        return InterpolationType(str(value))
    except ValueError as exc:
        raise TeachingAIProposalError(
            f"Unsupported interpolation: {value!r}"
        ) from exc


# --------------------------------------------------------------------------- #
# Extended command names
# --------------------------------------------------------------------------- #
#
# These extend the 4 base names.  The host dispatches a TeachingAICommand by
# trying the base registry first, then this one, so old code keeps working.

NAME_REMOVE_ELEMENT = "remove_element"
NAME_REPLACE_ELEMENT = "replace_element"
NAME_SET_DURATION = "set_duration"
NAME_SET_BACKGROUND = "set_background"
NAME_ADD_MARKER = "add_marker"
NAME_REMOVE_MARKER = "remove_marker"
NAME_SET_EASING = "set_easing"
NAME_INSERT_KEYFRAME = "insert_keyframe"
NAME_REMOVE_KEYFRAME = "remove_keyframe"
NAME_GROUP_ELEMENTS = "group_elements"
NAME_UNGROUP_ELEMENTS = "ungroup_elements"
NAME_SET_LAYOUT = "set_layout"
NAME_SET_COLOR_TOKEN = "set_color_token"
NAME_SET_TYPO_TOKEN = "set_typo_token"
NAME_ADD_AUDIO_REF = "add_audio_ref"
NAME_REMOVE_AUDIO_REF = "remove_audio_ref"
# TM-02/05: template + brand commands (delegate to core/template_commands).
NAME_APPLY_TEMPLATE = "apply_template"
NAME_SET_BRAND_THEME = "set_brand_theme"
NAME_REPLACE_ASSET_SLOT = "replace_asset_slot"
# TM-03/MT-05: motion-preset commands (delegate to motion/motion_commands,
# which bake the pure-function preset/spring into a KeyframeTrack so the
# existing evaluator renders springs/staggers without a render-path change).
NAME_APPLY_MOTION_PRESET = "apply_motion_preset"
NAME_STAGGER_ELEMENTS = "stagger_elements"

EXTENDED_COMMAND_NAMES: frozenset[str] = frozenset({
    NAME_REMOVE_ELEMENT,
    NAME_REPLACE_ELEMENT,
    NAME_SET_DURATION,
    NAME_SET_BACKGROUND,
    NAME_ADD_MARKER,
    NAME_REMOVE_MARKER,
    NAME_SET_EASING,
    NAME_INSERT_KEYFRAME,
    NAME_REMOVE_KEYFRAME,
    NAME_GROUP_ELEMENTS,
    NAME_UNGROUP_ELEMENTS,
    NAME_SET_LAYOUT,
    NAME_SET_COLOR_TOKEN,
    NAME_SET_TYPO_TOKEN,
    NAME_ADD_AUDIO_REF,
    NAME_REMOVE_AUDIO_REF,
    NAME_APPLY_TEMPLATE,
    NAME_SET_BRAND_THEME,
    NAME_REPLACE_ASSET_SLOT,
    NAME_APPLY_MOTION_PRESET,
    NAME_STAGGER_ELEMENTS,
})

#: Maximum element count after an edit (defence against a runaway model).
MAX_ELEMENTS_AFTER_EDIT = 200
#: Maximum duration in frames (10 minutes at 60 fps is plenty for an MG clip).
MAX_DURATION_FRAMES = 60 * 60 * 60


# --------------------------------------------------------------------------- #
# Extended registry
# --------------------------------------------------------------------------- #


class ExtendedCommandRegistry:
    """Compile the extended Motion Graphic command vocabulary.

    Instances are cheap and stateless; the host typically keeps one around and
    delegates to it after the base :class:`TeachingAICommandRegistry`.
    """

    def __init__(self, *, base_registry: TeachingAICommandRegistry | None = None) -> None:
        self._base = base_registry or TeachingAICommandRegistry()

    # ---------------- dispatch ---------------- #

    def compile(
        self,
        command: TeachingAICommand,
        scene: TeachingSceneDocument,
    ) -> SceneCommand | list[SceneCommand]:
        """Compile one command.

        Returns either a single :class:`SceneCommand` (most commands) or a
        list (``replace_element`` / ``group_elements`` expand to multiple
        core commands).  Unknown names raise :class:`TeachingAIProposalError`.
        """

        name = command.name
        if name not in EXTENDED_COMMAND_NAMES:
            # Let the base registry handle the original 4 names.
            return self._base.compile(command, scene)
        handler = getattr(self, f"_compile_{name}", None)
        if handler is None:  # pragma: no cover - defensive
            raise TeachingAIProposalError(f"Unsupported extended command: {name}")
        return handler(command.arguments, scene)

    # ---------------- drop-in registry API (list-aware) ---------------- #
    #
    # The base registry's ``dry_run``/``apply``/``undo_applied`` assume one
    # compiled SceneCommand per TeachingAICommand.  ``replace_element`` and
    # ``group_elements`` compile to a *list* of SceneCommands, so the extended
    # registry overrides the whole trio to stay balanced: it records how many
    # core commands each AI command expanded to and undoes exactly that many.
    # This makes the extended registry a drop-in superset of the base — the
    # host can swap ``TeachingAICommandRegistry()`` for
    # ``ExtendedCommandRegistry()`` and the 4 base names keep working while the
    # 16 extended names become reachable (plan TM-02 / MG-09).

    @staticmethod
    def _apply_compiled(
        compiled: SceneCommand | list[SceneCommand], scene: TeachingSceneDocument,
    ) -> tuple[TeachingSceneDocument, int]:
        """Apply a (possibly list) compile result; return (scene, count)."""
        if isinstance(compiled, list):
            for sub in compiled:
                scene = sub.apply(scene)
            return scene, len(compiled)
        return compiled.apply(scene), 1

    def create_scene(self, name: str = "Teaching Scene", **kwargs: Any) -> TeachingSceneDocument:
        """Model-independent scene creation entry point for AI dry-run callers."""
        return self._base.create_scene(name, **kwargs)

    def dry_run(
        self, scene: TeachingSceneDocument, commands: Sequence[TeachingAICommand],
    ) -> TeachingAIProposal:
        """Preview the effect of a command sequence as a TeachingAIProposal.

        List-aware version of the base registry's ``dry_run``: it propagates
        ``compiled_counts`` so a later ``apply``/``undo_applied`` executes and
        undoes the exact number of core commands.  Enforces
        ``MAX_ELEMENTS_AFTER_EDIT`` as a defence against a runaway model.
        """
        from .commands import diff_scenes  # local to avoid an editor-controller import cycle at module load
        current = scene
        compiled_counts: list[int] = []
        for command in commands:
            compiled = self.compile(command, current)
            current, count = self._apply_compiled(compiled, current)
            compiled_counts.append(count)
        if len(current.elements) > MAX_ELEMENTS_AFTER_EDIT:
            raise TeachingAIProposalError(
                f"Edit would produce {len(current.elements)} elements "
                f"(max {MAX_ELEMENTS_AFTER_EDIT})"
            )
        diff = diff_scenes(scene, current)
        diff_payload = {
            "base_hash": _hash_scene(scene),
            "proposed_hash": _hash_scene(current),
            "commands": [c.to_dict() for c in commands],
        }
        diff_hash = hashlib.sha256(
            json.dumps(diff_payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return TeachingAIProposal(
            _hash_scene(scene), scene, current, tuple(commands), diff, diff_hash,
            compiled_counts=tuple(compiled_counts),
        )

    def apply(
        self, proposal: TeachingAIProposal, controller: Any, *, confirmed: bool = False,
    ) -> TeachingSceneDocument:
        """Execute a confirmed proposal's commands on ``controller`` (list-aware)."""
        if not confirmed:
            raise TeachingAIProposalError("Teaching Animation AI proposal requires explicit confirmation")
        if _hash_scene(controller.scene) != proposal.base_hash:
            raise TeachingAIProposalError("scene changed after preview; refresh the AI diff before applying")
        for command in proposal.commands:
            compiled = self.compile(command, controller.scene)
            if isinstance(compiled, list):
                for sub in compiled:
                    controller.execute(sub)
            else:
                controller.execute(compiled)
        return controller.scene

    def undo_applied(
        self, proposal: TeachingAIProposal, controller: Any,
    ) -> TeachingSceneDocument:
        """Undo an applied proposal, popping one core command per compiled step."""
        if _hash_scene(controller.scene) != _hash_scene(proposal.proposed_scene):
            raise TeachingAIProposalError("scene changed after AI Apply; refusing to undo another editor change")
        # Base proposals carry no counts → one undo per AI command.  Extended
        # proposals carry the real expansion count per AI command.
        counts = proposal.compiled_counts or (1,) * len(proposal.commands)
        for _ in range(sum(counts)):
            if controller.undo() is None:
                raise TeachingAIProposalError("AI Apply history is incomplete")
        return controller.scene

    # ---------------- individual compilers ---------------- #

    def _compile_remove_element(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        element_id = str(args.get("element_id", ""))
        if scene.element_by_id(element_id) is None:
            raise TeachingAIProposalError(f"Element not found: {element_id}")
        return RemoveElementCommand(element_id)

    def _compile_replace_element(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> list[SceneCommand]:
        element_id = str(args.get("element_id", ""))
        raw = args.get("element")
        if scene.element_by_id(element_id) is None:
            raise TeachingAIProposalError(f"Element not found: {element_id}")
        if not isinstance(raw, Mapping):
            raise TeachingAIProposalError("replace_element.element must be an object")
        # Force the replacement to keep the same id so downstream references
        # (animations, asset refs) stay valid.
        payload = dict(raw)
        payload["element_id"] = element_id
        new_element = SceneElement.from_dict(payload)
        return [
            RemoveElementCommand(element_id),
            AddElementCommand(new_element),
        ]

    def _compile_set_duration(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        try:
            duration = int(args.get("duration_frames", 0))
        except (TypeError, ValueError) as exc:
            raise TeachingAIProposalError("set_duration.duration_frames must be an integer") from exc
        if duration <= 0 or duration > MAX_DURATION_FRAMES:
            raise TeachingAIProposalError(
                f"set_duration.duration_frames out of range (1..{MAX_DURATION_FRAMES})"
            )
        return SetDurationCommand(duration)

    def _compile_set_background(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        background = args.get("background")
        if not isinstance(background, Mapping):
            raise TeachingAIProposalError("set_background.background must be an object")
        # External LLM data is validated before it is persisted into the scene
        # document (AUD-26708): a background without a usable color key would
        # silently flow to the renderer as an unnamed painting instruction.
        color = background.get("color")
        if not (isinstance(color, str) and color.strip()):
            raise TeachingAIProposalError("set_background.background.color is required")
        return _SetBackgroundCommand(dict(background))

    def _compile_add_marker(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        raw = args.get("marker")
        if not isinstance(raw, Mapping):
            raise TeachingAIProposalError("add_marker.marker must be an object")
        marker = SceneMarker.from_dict(raw)
        return AddMarkerCommand(marker)

    def _compile_remove_marker(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        marker_id = str(args.get("marker_id", ""))
        if not any(marker.marker_id == marker_id for marker in scene.markers):
            raise TeachingAIProposalError(f"Marker not found: {marker_id}")
        return _RemoveMarkerCommand(marker_id)

    def _compile_set_easing(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        animation_id = str(args.get("animation_id", ""))
        easing = str(args.get("easing", ""))
        if not easing:
            raise TeachingAIProposalError("set_easing.easing is required")
        # Validate up front so an unknown easing is a stable compiler error.
        _coerce_interpolation(easing)
        animation = next((a for a in scene.animations if a.animation_id == animation_id), None)
        if animation is None:
            raise TeachingAIProposalError(f"Animation not found: {animation_id}")
        return _SetEasingCommand(animation_id, easing)

    def _compile_insert_keyframe(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        animation_id = str(args.get("animation_id", ""))
        animation = next((a for a in scene.animations if a.animation_id == animation_id), None)
        if animation is None:
            raise TeachingAIProposalError(f"Animation not found: {animation_id}")
        try:
            frame = int(args.get("frame"))
            value = float(args.get("value"))
        except (TypeError, ValueError) as exc:
            raise TeachingAIProposalError("insert_keyframe.frame/value must be numeric") from exc
        if frame < 0 or frame >= scene.duration_frames:
            raise TeachingAIProposalError("insert_keyframe.frame is outside the scene")
        return _InsertKeyframeCommand(animation_id, frame, value)

    def _compile_remove_keyframe(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        animation_id = str(args.get("animation_id", ""))
        animation = next((a for a in scene.animations if a.animation_id == animation_id), None)
        if animation is None:
            raise TeachingAIProposalError(f"Animation not found: {animation_id}")
        try:
            frame = int(args.get("frame"))
        except (TypeError, ValueError) as exc:
            raise TeachingAIProposalError("remove_keyframe.frame must be an integer") from exc
        return _RemoveKeyframeCommand(animation_id, frame)

    def _compile_group_elements(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> list[SceneCommand]:
        element_ids = args.get("element_ids", [])
        if not isinstance(element_ids, (list, tuple)) or len(element_ids) < 2:
            raise TeachingAIProposalError("group_elements.element_ids must list at least 2 ids")
        for eid in element_ids:
            if scene.element_by_id(str(eid)) is None:
                raise TeachingAIProposalError(f"Element not found: {eid}")
        # Group = align z_index of the listed elements so they stack together
        # at the top of the z order.  Real grouping (parent transform) is a
        # core-model change scheduled for a later batch; this keeps the
        # vocabulary stable now.
        target_z = max((element.z_index for element in scene.elements), default=0) + 1
        return [
            UpdateElementCommand(str(eid), {"z_index": target_z})
            for eid in element_ids
        ]

    def _compile_ungroup_elements(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> list[SceneCommand]:
        element_ids = args.get("element_ids", [])
        if not isinstance(element_ids, (list, tuple)) or not element_ids:
            raise TeachingAIProposalError("ungroup_elements.element_ids must be a non-empty list")
        # Spread z_index back to the element list position so they no longer
        # share the grouped layer.
        return [
            UpdateElementCommand(str(eid), {"z_index": index})
            for index, eid in enumerate(element_ids)
        ]

    def _compile_set_layout(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> list[SceneCommand]:
        layout = args.get("layout")
        if not isinstance(layout, Mapping):
            raise TeachingAIProposalError("set_layout.layout must be an object")
        elements_layout = layout.get("elements")
        if not isinstance(elements_layout, (list, tuple)):
            raise TeachingAIProposalError("set_layout.layout.elements must be an array")
        commands: list[SceneCommand] = []
        for item in elements_layout:
            if not isinstance(item, Mapping):
                raise TeachingAIProposalError("layout element entry must be an object")
            element_id = str(item.get("element_id", ""))
            if scene.element_by_id(element_id) is None:
                raise TeachingAIProposalError(f"Element not found: {element_id}")
            transform_payload = item.get("transform")
            if not isinstance(transform_payload, Mapping):
                raise TeachingAIProposalError(f"layout entry for {element_id} needs a transform")
            transform = SceneTransform.from_dict(transform_payload)
            commands.append(UpdateElementCommand(element_id, {"transform": transform}))
        return commands

    def _compile_set_color_token(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> list[SceneCommand]:
        token = str(args.get("token", ""))
        color = args.get("color")
        if not token or not isinstance(color, str):
            raise TeachingAIProposalError("set_color_token.token/color must be strings")
        # Apply the colour to every element whose style references the token.
        # The scene token convention is ``$category.key`` (core/design_tokens);
        # scenes built from templates usually hold the already-resolved value,
        # so both the raw reference and the token's current value match.
        ref = f"${token}"
        current = read_scene_tokens(scene.metadata).resolve_value(ref)
        candidates = {ref, str(current)}
        matches = [
            element for element in scene.elements
            if isinstance(element.style, Mapping)
            and any(
                isinstance(value, str)
                and value.lower() in {candidate.lower() for candidate in candidates}
                for value in (element.style.get("color"), element.style.get("fill"))
            )
        ]
        if not matches:
            raise TeachingAIProposalError(
                f"No elements reference color token {token!r} "
                f"(looked for {ref!r} and resolved value {current!r})"
            )
        return [
            UpdateElementCommand(element.element_id, {"style": {**dict(element.style), "color": color, "fill": color}})
            for element in matches
        ]

    def _compile_set_typo_token(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> list[SceneCommand]:
        token = str(args.get("token", ""))
        font_family = args.get("font_family")
        font_size = args.get("font_size")
        if not token:
            raise TeachingAIProposalError("set_typo_token.token is required")
        if not font_family and font_size is None:
            raise TeachingAIProposalError("set_typo_token needs font_family or font_size")
        # Typo tokens live in the ``type_scale`` category (``$type_scale.title``
        # etc.).  Match both the raw ``$`` reference and the resolved size, so
        # template-produced scenes (styles resolved to concrete numbers) are
        # still addressable by token name.
        ref = f"${token}"
        resolved = read_scene_tokens(scene.metadata).resolve_value(ref)
        resolved_size = resolved if isinstance(resolved, (int, float)) else None

        def _uses_token(value: Any) -> bool:
            if isinstance(value, str):
                return value == ref
            if resolved_size is None or isinstance(value, bool):
                return False
            try:
                return float(value) == float(resolved_size)
            except (TypeError, ValueError):
                return False

        matches = [
            element for element in scene.elements
            if isinstance(element.style, Mapping) and _uses_token(element.style.get("font_size"))
        ]
        if not matches:
            raise TeachingAIProposalError(
                f"No elements reference typo token {token!r} "
                f"(looked for {ref!r} and resolved value {resolved!r})"
            )
        commands: list[SceneCommand] = []
        for element in matches:
            new_style = dict(element.style)
            if font_family:
                new_style["font_family"] = str(font_family)
            if font_size is not None:
                try:
                    new_style["font_size"] = float(font_size)
                except (TypeError, ValueError) as exc:
                    raise TeachingAIProposalError("set_typo_token.font_size must be numeric") from exc
            commands.append(UpdateElementCommand(element.element_id, {"style": new_style}))
        return commands

    def _compile_add_audio_ref(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        audio = args.get("audio")
        if not isinstance(audio, Mapping):
            raise TeachingAIProposalError("add_audio_ref.audio must be an object")
        # An audio ref without an id cannot be addressed by remove_audio_ref and
        # cannot be traced back to media — reject instead of persisting (AUD-26708).
        audio_id = str(audio.get("audio_id", "") or "")
        if not audio_id:
            raise TeachingAIProposalError("add_audio_ref.audio.audio_id is required")
        return _AddAudioRefCommand(dict(audio))

    def _compile_remove_audio_ref(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        audio_id = str(args.get("audio_id", ""))
        return _RemoveAudioRefCommand(audio_id)

    # ---------------- template + brand commands (TM-02/05) ---------------- #
    #
    # These delegate to the immutable command classes in core/template_commands,
    # which own template instantiation/compilation, brand re-themeing and asset-
    # slot swapping.  They make the 15 built-in executable templates and the
    # design-token system reachable from the AI command vocabulary (the audit
    # found both built and tested but with no production caller).

    def _compile_apply_template(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        template_id = str(args.get("template_id", ""))
        if not template_id:
            raise TeachingAIProposalError("apply_template.template_id is required")
        params = args.get("params")
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            raise TeachingAIProposalError("apply_template.params must be an object")
        return ApplyTemplateCommand(template_id, dict(params))

    def _compile_set_brand_theme(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        theme_raw = args.get("theme")
        if not isinstance(theme_raw, Mapping):
            raise TeachingAIProposalError("set_brand_theme.theme must be an object")
        theme_id = str(theme_raw.get("theme_id", "") or "")
        if not theme_id:
            raise TeachingAIProposalError("set_brand_theme.theme.theme_id is required")

        def _cast_map(key: str) -> Mapping[str, Any]:
            """Coerce an optional theme mapping; a present-but-invalid value is
            a hard error instead of a silent ``{}`` that drops the model's
            requested theme (AUD-26708)."""

            value = theme_raw.get(key)
            if value is None:
                return {}
            if not isinstance(value, Mapping):
                raise TeachingAIProposalError(
                    f"set_brand_theme.theme.{key} must be an object",
                )
            return dict(value)

        safe_area = theme_raw.get("safe_area")
        if safe_area is not None and not (isinstance(safe_area, (tuple, list)) and len(safe_area) == 4):
            raise TeachingAIProposalError("set_brand_theme.theme.safe_area must be 4 numbers")
        theme = BrandTheme(
            theme_id=theme_id,
            palette=_cast_map("palette"),
            type_scale=_cast_map("type_scale"),
            spacing=_cast_map("spacing"),
            motion=_cast_map("motion"),
            safe_area=tuple(float(v) for v in safe_area) if safe_area is not None else (),
            source=str(theme_raw.get("source", "") or ""),
        )
        return SetBrandThemeCommand(theme)

    def _compile_replace_asset_slot(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        element_id = str(args.get("element_id", ""))
        if scene.element_by_id(element_id) is None:
            raise TeachingAIProposalError(f"Element not found: {element_id}")
        asset_raw = args.get("asset")
        if not isinstance(asset_raw, Mapping):
            raise TeachingAIProposalError("replace_asset_slot.asset must be an object")
        asset = SceneAssetRef.from_dict(asset_raw)
        return ReplaceAssetSlotCommand(element_id, asset)

    # ---------------- motion presets (TM-03 / MT-05) ---------------- #
    #
    # These delegate to motion/motion_commands, which bake the pure-function
    # preset (fade/slide/pop/scale/overshoot/reveal/draw/...) or spring into a
    # KeyframeTrack via compile_preset_to_track.  The existing evaluator reads
    # KeyframeTrack, so springs/staggers now affect rendered pixels without any
    # change to the render path — closing the "motion primitives orphaned from
    # the render path" gap the audit found.

    def _compile_apply_motion_preset(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        element_id = str(args.get("element_id", ""))
        if scene.element_by_id(element_id) is None:
            raise TeachingAIProposalError(f"Element not found: {element_id}")
        preset_id = str(args.get("preset_id", "") or "")
        if not preset_id:
            raise TeachingAIProposalError("apply_motion_preset.preset_id is required")
        param_name = str(args.get("param_name", "") or "")
        if not param_name:
            raise TeachingAIProposalError("apply_motion_preset.param_name is required")
        start_frame = int(args.get("start_frame", 0) or 0)
        if start_frame < 0 or start_frame >= scene.duration_frames:
            raise TeachingAIProposalError("apply_motion_preset.start_frame is outside the scene")
        end_frame = args.get("end_frame")
        end_frame = int(end_frame) if end_frame is not None else None
        params = args.get("params")
        if params is not None and not isinstance(params, Mapping):
            raise TeachingAIProposalError("apply_motion_preset.params must be an object")
        return ApplyMotionPresetCommand(
            element_id=element_id, preset_id=preset_id, param_name=param_name,
            start_frame=start_frame, end_frame=end_frame,
            params=dict(params) if isinstance(params, Mapping) else {},
        )

    def _compile_stagger_elements(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        raw_ids = args.get("element_ids")
        if not isinstance(raw_ids, (list, tuple)) or not raw_ids:
            raise TeachingAIProposalError("stagger_elements.element_ids must be a non-empty array")
        element_ids = tuple(str(eid) for eid in raw_ids)
        for eid in element_ids:
            if scene.element_by_id(eid) is None:
                raise TeachingAIProposalError(f"Element not found: {eid}")
        preset_id = str(args.get("preset_id", "") or "")
        if not preset_id:
            raise TeachingAIProposalError("stagger_elements.preset_id is required")
        param_name = str(args.get("param_name", "") or "")
        if not param_name:
            raise TeachingAIProposalError("stagger_elements.param_name is required")
        # Explicit ``None`` means "use the default"; an explicit 0 is a valid
        # value (interval=0 → all targets animate simultaneously, duration=0
        # must hit the positive-duration check below), so never coerce with
        # ``or`` which would silently rewrite 0 to the default.
        raw_interval = args.get("interval", 12)
        interval = 12 if raw_interval is None else int(raw_interval)
        raw_duration = args.get("duration", 18)
        duration = 18 if raw_duration is None else int(raw_duration)
        if interval < 0:
            raise TeachingAIProposalError("stagger_elements.interval must be non-negative")
        if duration <= 0:
            raise TeachingAIProposalError("stagger_elements.duration must be positive")
        params = args.get("params")
        if params is not None and not isinstance(params, Mapping):
            raise TeachingAIProposalError("stagger_elements.params must be an object")
        return StaggerElementsCommand(
            element_ids=element_ids, preset_id=preset_id, param_name=param_name,
            interval=interval, duration=duration,
            delay=int(args.get("delay", 0) or 0),
            anchor_frame=int(args.get("anchor_frame", 0) or 0),
            params=dict(params) if isinstance(params, Mapping) else {},
        )


# --------------------------------------------------------------------------- #
# Custom scene-level commands (background / markers / audio refs)
# --------------------------------------------------------------------------- #
#
# The base core/commands module does not yet have commands for background or
# audio refs.  Rather than mutate the core module from the AI layer, we define
# small command types here that implement the SceneCommand protocol.  They
# keep undo/redo semantics identical to the existing commands.


from dataclasses import dataclass as _dataclass
import copy as _copy
from dataclasses import field as _field
import uuid as _uuid


def _cmd_id() -> str:
    return str(_uuid.uuid4())


@_dataclass(frozen=True)
class _SetBackgroundCommand:
    background: Mapping[str, Any]
    command_id: str = _field(default_factory=_cmd_id)
    label: str = "Set Scene Background"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        previous = scene.background
        object.__setattr__(self, "_previous", previous)
        return _replace(scene, background=_copy.deepcopy(dict(self.background)))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        previous = getattr(self, "_previous", None)
        if previous is None:
            raise RuntimeError("background command has not been applied")
        return _replace(scene, background=previous)


@_dataclass(frozen=True)
class _RemoveMarkerCommand:
    marker_id: str
    removed_marker: SceneMarker | None = None
    command_id: str = _field(default_factory=_cmd_id)
    label: str = "Remove Scene Marker"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        marker = next((m for m in scene.markers if m.marker_id == self.marker_id), None)
        if marker is None:
            raise ValueError(f"Marker not found: {self.marker_id}")
        object.__setattr__(self, "removed_marker", marker)
        new_markers = tuple(m for m in scene.markers if m.marker_id != self.marker_id)
        return _replace(scene, markers=new_markers)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self.removed_marker is None:
            raise RuntimeError("remove marker command has not been applied")
        return scene.with_marker_added(self.removed_marker)


@_dataclass(frozen=True)
class _SetEasingCommand:
    animation_id: str
    easing: str
    previous: tuple[str, ...] | None = None
    command_id: str = _field(default_factory=_cmd_id)
    label: str = "Set Animation Easing"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        animation = next((a for a in scene.animations if a.animation_id == self.animation_id), None)
        if animation is None:
            raise ValueError(f"Animation not found: {self.animation_id}")
        previous = tuple(kf.interpolation for kf in animation.track.keyframes)
        object.__setattr__(self, "previous", previous)
        easing_enum = _coerce_interpolation(self.easing)
        new_keyframes = tuple(
            type(kf)(kf.frame, kf.value, easing_enum) for kf in animation.track.keyframes
        )
        new_track = KeyframeTrack(animation.track.param_name, min_val=animation.track.min_val, max_val=animation.track.max_val)
        for kf in new_keyframes:
            new_track.add_keyframe(kf)
        new_animation = SceneAnimation(animation.animation_id, animation.element_id, animation.property, new_track)
        new_animations = tuple(
            new_animation if a.animation_id == self.animation_id else a
            for a in scene.animations
        )
        return _replace(scene, animations=new_animations)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        animation = next((a for a in scene.animations if a.animation_id == self.animation_id), None)
        if animation is None or self.previous is None:
            raise RuntimeError("easing command has not been applied")
        keyframes = list(animation.track.keyframes)
        restored = KeyframeTrack(animation.track.param_name, min_val=animation.track.min_val, max_val=animation.track.max_val)
        for kf, interp in zip(keyframes, self.previous):
            restored.add_keyframe(type(kf)(kf.frame, kf.value, interp))
        new_animation = SceneAnimation(animation.animation_id, animation.element_id, animation.property, restored)
        new_animations = tuple(
            new_animation if a.animation_id == self.animation_id else a
            for a in scene.animations
        )
        return _replace(scene, animations=new_animations)


@_dataclass(frozen=True)
class _InsertKeyframeCommand:
    animation_id: str
    frame: int
    value: float
    command_id: str = _field(default_factory=_cmd_id)
    label: str = "Insert Keyframe"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        animation = next((a for a in scene.animations if a.animation_id == self.animation_id), None)
        if animation is None:
            raise ValueError(f"Animation not found: {self.animation_id}")
        existing = next((kf for kf in animation.track.keyframes if kf.frame == self.frame), None)
        object.__setattr__(self, "_existed", existing)
        new_track = KeyframeTrack(animation.track.param_name, min_val=animation.track.min_val, max_val=animation.track.max_val)
        inserted = False
        for kf in animation.track.keyframes:
            if not inserted and self.frame < kf.frame:
                new_track.add_keyframe(Keyframe(self.frame, self.value))
                inserted = True
            new_track.add_keyframe(kf)
        if not inserted:
            new_track.add_keyframe(Keyframe(self.frame, self.value))
        new_animation = SceneAnimation(animation.animation_id, animation.element_id, animation.property, new_track)
        new_animations = tuple(
            new_animation if a.animation_id == self.animation_id else a
            for a in scene.animations
        )
        return _replace(scene, animations=new_animations)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        animation = next((a for a in scene.animations if a.animation_id == self.animation_id), None)
        if animation is None:
            raise RuntimeError("insert keyframe command has not been applied")
        existed = getattr(self, "_existed", None)
        new_track = KeyframeTrack(animation.track.param_name, min_val=animation.track.min_val, max_val=animation.track.max_val)
        # Drop whatever now sits at this frame (the inserted keyframe replaced
        # the original in place, see KeyframeTrack.add_keyframe), then restore
        # the original keyframe recorded at apply time so undo returns the
        # track to its exact pre-command state instead of losing the frame.
        for kf in animation.track.keyframes:
            if kf.frame == self.frame:
                continue
            new_track.add_keyframe(kf)
        if existed is not None:
            new_track.add_keyframe(existed)
        new_animation = SceneAnimation(animation.animation_id, animation.element_id, animation.property, new_track)
        new_animations = tuple(
            new_animation if a.animation_id == self.animation_id else a
            for a in scene.animations
        )
        return _replace(scene, animations=new_animations)


@_dataclass(frozen=True)
class _RemoveKeyframeCommand:
    animation_id: str
    frame: int
    removed: Keyframe | None = None
    command_id: str = _field(default_factory=_cmd_id)
    label: str = "Remove Keyframe"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        animation = next((a for a in scene.animations if a.animation_id == self.animation_id), None)
        if animation is None:
            raise ValueError(f"Animation not found: {self.animation_id}")
        removed = next((kf for kf in animation.track.keyframes if kf.frame == self.frame), None)
        if removed is None:
            raise ValueError(f"Keyframe not found at frame {self.frame}")
        object.__setattr__(self, "removed", removed)
        new_track = KeyframeTrack(animation.track.param_name, min_val=animation.track.min_val, max_val=animation.track.max_val)
        for kf in animation.track.keyframes:
            if kf.frame != self.frame:
                new_track.add_keyframe(kf)
        new_animation = SceneAnimation(animation.animation_id, animation.element_id, animation.property, new_track)
        new_animations = tuple(
            new_animation if a.animation_id == self.animation_id else a
            for a in scene.animations
        )
        return _replace(scene, animations=new_animations)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        animation = next((a for a in scene.animations if a.animation_id == self.animation_id), None)
        if animation is None or self.removed is None:
            raise RuntimeError("remove keyframe command has not been applied")
        new_track = KeyframeTrack(animation.track.param_name, min_val=animation.track.min_val, max_val=animation.track.max_val)
        inserted = False
        for kf in animation.track.keyframes:
            if not inserted and self.removed.frame < kf.frame:
                new_track.add_keyframe(self.removed)
                inserted = True
            new_track.add_keyframe(kf)
        if not inserted:
            new_track.add_keyframe(self.removed)
        new_animation = SceneAnimation(animation.animation_id, animation.element_id, animation.property, new_track)
        new_animations = tuple(
            new_animation if a.animation_id == self.animation_id else a
            for a in scene.animations
        )
        return _replace(scene, animations=new_animations)


@_dataclass(frozen=True)
class _AddAudioRefCommand:
    audio: Mapping[str, Any]
    command_id: str = _field(default_factory=_cmd_id)
    label: str = "Add Audio Reference"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        metadata = dict(scene.metadata)
        audio_refs = list(metadata.get("audio_refs", []))
        audio_refs.append(_copy.deepcopy(dict(self.audio)))
        metadata["audio_refs"] = audio_refs
        return _replace(scene, metadata=metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        metadata = dict(scene.metadata)
        audio_refs = list(metadata.get("audio_refs", []))
        # Remove the last matching entry.
        for index in range(len(audio_refs) - 1, -1, -1):
            if audio_refs[index] == dict(self.audio):
                del audio_refs[index]
                break
        metadata["audio_refs"] = audio_refs
        return _replace(scene, metadata=metadata)


@_dataclass(frozen=True)
class _RemoveAudioRefCommand:
    audio_id: str
    removed: Mapping[str, Any] | None = None
    command_id: str = _field(default_factory=_cmd_id)
    label: str = "Remove Audio Reference"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        metadata = dict(scene.metadata)
        audio_refs = list(metadata.get("audio_refs", []))
        target = None
        for entry in audio_refs:
            if isinstance(entry, Mapping) and entry.get("audio_id") == self.audio_id:
                target = entry
                break
        if target is None:
            raise ValueError(f"Audio reference not found: {self.audio_id}")
        object.__setattr__(self, "removed", target)
        metadata["audio_refs"] = [entry for entry in audio_refs if entry is not target]
        return _replace(scene, metadata=metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from dataclasses import replace as _replace
        metadata = dict(scene.metadata)
        audio_refs = list(metadata.get("audio_refs", []))
        if self.removed is None:
            raise RuntimeError("remove audio command has not been applied")
        audio_refs.append(_copy.deepcopy(dict(self.removed)))
        metadata["audio_refs"] = audio_refs
        return _replace(scene, metadata=metadata)


# --------------------------------------------------------------------------- #
# Dry-run helper
# --------------------------------------------------------------------------- #


def dry_run_extended(
    registry: ExtendedCommandRegistry,
    scene: TeachingSceneDocument,
    commands: Sequence[TeachingAICommand],
) -> TeachingSceneDocument:
    """Apply a sequence of (possibly extended) commands and return the result.

    Unlike the base registry's ``dry_run``, this returns the final scene
    directly — the base registry owns the proposal/diff/hash wrapping.  The
    host uses this when it wants to preview the *effect* of an extended edit
    before showing it on the card.
    """

    current = scene
    for command in commands:
        compiled = registry.compile(command, current)
        if isinstance(compiled, list):
            for sub in compiled:
                current = sub.apply(current)
        else:
            current = compiled.apply(current)
    if len(current.elements) > MAX_ELEMENTS_AFTER_EDIT:
        raise TeachingAIProposalError(
            f"Edit would produce {len(current.elements)} elements "
            f"(max {MAX_ELEMENTS_AFTER_EDIT})"
        )
    return current


__all__ = [
    "ExtendedCommandRegistry",
    "EXTENDED_COMMAND_NAMES",
    "MAX_ELEMENTS_AFTER_EDIT",
    "MAX_DURATION_FRAMES",
    "NAME_REMOVE_ELEMENT",
    "NAME_REPLACE_ELEMENT",
    "NAME_SET_DURATION",
    "NAME_SET_BACKGROUND",
    "NAME_ADD_MARKER",
    "NAME_REMOVE_MARKER",
    "NAME_SET_EASING",
    "NAME_INSERT_KEYFRAME",
    "NAME_REMOVE_KEYFRAME",
    "NAME_GROUP_ELEMENTS",
    "NAME_UNGROUP_ELEMENTS",
    "NAME_SET_LAYOUT",
    "NAME_SET_COLOR_TOKEN",
    "NAME_SET_TYPO_TOKEN",
    "NAME_ADD_AUDIO_REF",
    "NAME_REMOVE_AUDIO_REF",
    "NAME_APPLY_TEMPLATE",
    "NAME_SET_BRAND_THEME",
    "NAME_REPLACE_ASSET_SLOT",
    "NAME_APPLY_MOTION_PRESET",
    "NAME_STAGGER_ELEMENTS",
    "dry_run_extended",
]
