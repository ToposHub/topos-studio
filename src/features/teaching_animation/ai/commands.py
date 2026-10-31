"""Reviewable Teaching Animation AI commands and apply/undo transaction."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from ..core.commands import (
    AddAnimationCommand,
    AddElementCommand,
    ReorderElementCommand,
    SceneCommand,
    UpdateElementCommand,
)
from ..core.model import SceneAnimation, SceneElement, TeachingSceneDocument
from ..ui.editor_controller import SceneEditorController, SceneDiff, diff_scenes
from src.shared.keyframe import Keyframe, KeyframeTrack


def _hash_scene(scene: TeachingSceneDocument) -> str:
    payload = json.dumps(scene.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TeachingAICommand:
    name: str
    arguments: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": dict(self.arguments)}


@dataclass(frozen=True)
class TeachingAIProposal:
    base_hash: str
    base_scene: TeachingSceneDocument
    proposed_scene: TeachingSceneDocument
    commands: tuple[TeachingAICommand, ...]
    diff: SceneDiff
    diff_hash: str
    #: How many core SceneCommands each TeachingAICommand compiled to.  The
    #: base registry produces one per command (so the default empty tuple is
    #: treated as all-ones); the extended registry may compile a single AI
    #: command to a list (e.g. ``replace_element`` → remove+add), and apply/
    #: undo must stay balanced against that count, not the AI-command count.
    compiled_counts: tuple[int, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.diff.is_empty


class TeachingAIProposalError(ValueError):
    pass


class TeachingAICommandRegistry:
    """Compile a small stable command vocabulary without requiring an LLM."""

    def compile(self, command: TeachingAICommand, scene: TeachingSceneDocument) -> SceneCommand:
        args = command.arguments
        if command.name == "add_element":
            raw = args.get("element")
            if not isinstance(raw, Mapping):
                raise TeachingAIProposalError("add_element.element must be an object")
            return AddElementCommand(SceneElement.from_dict(raw), args.get("index"))
        if command.name == "update_element":
            element_id = str(args.get("element_id", ""))
            updates = args.get("updates", {})
            if not isinstance(updates, Mapping):
                raise TeachingAIProposalError("update_element.updates must be an object")
            if scene.element_by_id(element_id) is None:
                raise TeachingAIProposalError(f"Element not found: {element_id}")
            return UpdateElementCommand(element_id, dict(updates))
        if command.name == "reorder_element":
            return ReorderElementCommand(str(args.get("element_id", "")), int(args.get("index", 0)))
        if command.name == "set_animation":
            element_id = str(args.get("element_id", ""))
            property_name = str(args.get("property", ""))
            keyframes = args.get("keyframes", [])
            if not isinstance(keyframes, Sequence) or isinstance(keyframes, (str, bytes)):
                raise TeachingAIProposalError("set_animation.keyframes must be an array")
            track = KeyframeTrack(property_name, min_val=float(args.get("min_val", 0.0)), max_val=float(args.get("max_val", 1.0)))
            for item in keyframes:
                if not isinstance(item, Mapping):
                    raise TeachingAIProposalError("keyframe must be an object")
                track.add_keyframe(Keyframe(int(item["frame"]), float(item["value"])))
            return AddAnimationCommand(SceneAnimation(str(args.get("animation_id", f"ai_{element_id}_{property_name}")), element_id, property_name, track))
        raise TeachingAIProposalError(f"Unsupported Teaching Animation command: {command.name}")

    def create_scene(self, name: str = "Teaching Scene", **kwargs: Any) -> TeachingSceneDocument:
        """Model-independent scene creation entry point for AI dry-run callers."""
        return TeachingSceneDocument.create_default(name=name, **kwargs)

    def dry_run(self, scene: TeachingSceneDocument, commands: Sequence[TeachingAICommand]) -> TeachingAIProposal:
        current = scene
        for command in commands:
            current = self.compile(command, current).apply(current)
        diff = diff_scenes(scene, current)
        diff_payload = {"base_hash": _hash_scene(scene), "proposed_hash": _hash_scene(current), "commands": [command.to_dict() for command in commands]}
        diff_hash = hashlib.sha256(json.dumps(diff_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return TeachingAIProposal(_hash_scene(scene), scene, current, tuple(commands), diff, diff_hash)

    def apply(self, proposal: TeachingAIProposal, controller: SceneEditorController, *, confirmed: bool = False) -> TeachingSceneDocument:
        if not confirmed:
            raise TeachingAIProposalError("Teaching Animation AI proposal requires explicit confirmation")
        if _hash_scene(controller.scene) != proposal.base_hash:
            raise TeachingAIProposalError("scene changed after preview; refresh the AI diff before applying")
        for command in proposal.commands:
            controller.execute(self.compile(command, controller.scene))
        return controller.scene

    def undo_applied(self, proposal: TeachingAIProposal, controller: SceneEditorController) -> TeachingSceneDocument:
        if _hash_scene(controller.scene) != _hash_scene(proposal.proposed_scene):
            raise TeachingAIProposalError("scene changed after AI Apply; refusing to undo another editor change")
        for _ in proposal.commands:
            if controller.undo() is None:
                raise TeachingAIProposalError("AI Apply history is incomplete")
        return controller.scene
