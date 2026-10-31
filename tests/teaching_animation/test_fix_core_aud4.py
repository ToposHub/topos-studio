"""Regression tests for the batch-A Critical audit fixes (w4-g1).

Covers:
  * AUD-27018 — ``TeachingAnimationAIService.replace_clip`` must route the
    removal of the original clip through the overwrite insert command so the
    host undo stack restores it (clip + scene-reference manifest); the old
    pre-deletion ran outside the command and made the loss permanent.
  * AUD-27400 — ``RemoveElementCommand.undo`` must restore the animation
    tracks that ``with_element_removed`` drops together with the element.
  * AUD-27704 — template-package import must reject template ids that are
    not one safe path segment (path escape out of the store root / arbitrary
    ``rmtree`` via ``overwrite=True``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.features.edit.core.commands import CommandStack as EditCommandStack
from src.features.edit.core.timeline_v2 import TimelineSequence
from src.features.teaching_animation.ai.service import TeachingAnimationAIService
from src.features.teaching_animation.core.commands import (
    RemoveElementCommand,
    SceneCommandStack,
)
from src.features.teaching_animation.core.model import (
    SceneAnimation,
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.core.motion_graphic_template_library import (
    default_motion_graphic_template_root,
)
from src.features.teaching_animation.core.motion_graphic_template_store import (
    TemplateStoreError,
    import_template_folder,
)
from src.features.teaching_animation.host_adapters import get_scene_reference_manifest
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack


def _scene() -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default(
        "AI lesson", width=64, height=48, duration_frames=24)


# ------------------------------------------------------------------ AUD-27018

def test_replace_clip_undo_restores_original_clip_and_manifest() -> None:
    """Undo after replace_clip must bring the original clip back (AUD-27018)."""
    service = TeachingAnimationAIService(_scene())
    timeline = TimelineSequence(timeline_id="replace-undo")
    inserted = service.insert_into_timeline(timeline, confirmed=True)
    original_id = inserted.clip.clip_id

    stack = EditCommandStack()
    replaced = service.replace_clip(
        inserted.timeline, original_id, confirmed=True, command_stack=stack)
    new_id = replaced.clip.clip_id
    assert new_id != original_id
    assert replaced.timeline.get_clip_by_id(original_id) is None
    assert replaced.clip.timeline_start == inserted.clip.timeline_start
    assert replaced.clip.track_id == inserted.clip.track_id

    undone = stack.undo(replaced.timeline)
    assert undone is not None
    assert undone.get_clip_by_id(new_id) is None, "undo removed the replacement"
    restored = undone.get_clip_by_id(original_id)
    assert restored is not None, "undo must restore the replaced original clip"
    assert restored.timeline_start == inserted.clip.timeline_start
    assert get_scene_reference_manifest(undone, original_id) is not None, (
        "undo must restore the original clip's scene-reference manifest")


def test_replace_clip_requires_confirmation_and_unknown_clip() -> None:
    service = TeachingAnimationAIService(_scene())
    timeline = TimelineSequence(timeline_id="replace-gate")
    inserted = service.insert_into_timeline(timeline, confirmed=True)
    with pytest.raises(PermissionError):
        service.replace_clip(inserted.timeline, inserted.clip.clip_id)
    with pytest.raises(KeyError):
        service.replace_clip(inserted.timeline, "missing-clip", confirmed=True)


# ------------------------------------------------------------------ AUD-27400

def _element_with_animation(scene: TeachingSceneDocument) -> tuple[TeachingSceneDocument, str]:
    element = SceneElement(
        element_id="element-with-track",
        type="shape",
        transform=SceneTransform(x=10.0, y=10.0, width=20.0, height=20.0),
        content={"kind": "circle"},
    )
    track = KeyframeTrack(param_name="opacity", min_val=0.0, max_val=1.0)
    track.add_keyframe(Keyframe(0, 0.0, InterpolationType.EASE_OUT))
    track.add_keyframe(Keyframe(12, 1.0, InterpolationType.LINEAR))
    animation = SceneAnimation("animation-1", element.element_id, "opacity", track)
    return scene.with_element_added(element).with_animation_added(animation), element.element_id


def test_remove_element_undo_restores_animation_tracks() -> None:
    """Undo after element removal must restore its keyframe tracks (AUD-27400)."""
    scene, element_id = _element_with_animation(_scene())
    stack = SceneCommandStack()
    after = stack.execute(RemoveElementCommand(element_id), scene)
    assert after.element_by_id(element_id) is None
    assert not [a for a in after.animations if a.element_id == element_id], (
        "with_element_removed drops the element's animation tracks")

    undone = stack.undo(after)
    assert undone is not None
    assert undone.element_by_id(element_id) is not None, "element restored"
    assert undone.elements == scene.elements, "element order fully restored"
    restored_tracks = sorted(
        (a for a in undone.animations if a.element_id == element_id),
        key=lambda a: a.animation_id)
    original_tracks = sorted(
        (a for a in scene.animations if a.element_id == element_id),
        key=lambda a: a.animation_id)
    assert [a.to_dict() for a in restored_tracks] == [a.to_dict() for a in original_tracks], (
        "undo must restore the animation tracks removed with the element")

    # redo removes element and tracks again — the command stays symmetric.
    redone = stack.redo(undone)
    assert redone is not None
    assert redone.element_by_id(element_id) is None
    assert not [a for a in redone.animations if a.element_id == element_id]


def test_remove_element_undo_restores_multiple_tracks_in_order() -> None:
    scene = _scene()
    element = SceneElement(
        element_id="multi",
        type="shape",
        transform=SceneTransform(x=0.0, y=0.0, width=8.0, height=8.0),
        content={"kind": "rect"},
    )
    scene = scene.with_element_added(element)
    for index, prop in enumerate(("opacity", "transform.x")):
        track = KeyframeTrack(param_name=prop, min_val=0.0, max_val=100.0)
        track.add_keyframe(Keyframe(0, 0.0, InterpolationType.LINEAR))
        scene = scene.with_animation_added(
            SceneAnimation(f"anim-{index}", element.element_id, prop, track))
    command = RemoveElementCommand(element.element_id)
    after = command.apply(scene)
    assert len(after.animations) == len(scene.animations) - 2
    undone = command.undo(after)
    assert sorted(a.animation_id for a in undone.animations) == ["anim-0", "anim-1"]


# ------------------------------------------------------------------ AUD-27704

BUNDLED_PACKAGE = default_motion_graphic_template_root() / "particle_flow_background"


def _make_package(dest: Path, template_id: str) -> Path:
    """Materialize a loadable package by copying the real bundled one."""
    dest.mkdir(parents=True, exist_ok=True)
    for item in BUNDLED_PACKAGE.iterdir():
        if item.is_file():
            (dest / item.name).write_bytes(item.read_bytes())
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    manifest["template_id"] = template_id
    (dest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return dest


@pytest.mark.parametrize("bad_id", ["../escape", "sub/dir", "..", ".", "a..b", "with space"])
def test_import_rejects_unsafe_template_ids(tmp_path: Path, bad_id: str) -> None:
    """A manifest id that is not one safe segment must never reach the disk."""
    user_root = tmp_path / "user"
    user_root.mkdir()
    source = _make_package(tmp_path / "incoming" / "evil", bad_id)
    with pytest.raises(TemplateStoreError, match="template id"):
        import_template_folder(source, user_root)
    assert list(user_root.iterdir()) == [], "rejected ids must leave the root untouched"
    assert not (tmp_path / "escape").exists(), "no write outside the store root"


def test_import_overwrite_cannot_rmtree_outside_root(tmp_path: Path) -> None:
    """overwrite=True must not delete an arbitrary directory via '..' ids."""
    user_root = tmp_path / "user"
    user_root.mkdir()
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "keep.txt").write_text("data", encoding="utf-8")
    source = _make_package(tmp_path / "incoming" / "evil", "../precious")
    with pytest.raises(TemplateStoreError, match="template id"):
        import_template_folder(source, user_root, overwrite=True)
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "data"


def test_import_still_accepts_whitelisted_ids(tmp_path: Path) -> None:
    user_root = tmp_path / "user"
    user_root.mkdir()
    source = _make_package(tmp_path / "incoming" / "pkg", "My_Template-01.v2")
    stored = import_template_folder(source, user_root)
    assert stored.template_id == "My_Template-01.v2"
    assert (user_root / "My_Template-01.v2" / "manifest.json").is_file()
