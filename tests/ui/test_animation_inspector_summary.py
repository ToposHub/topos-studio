"""R2: the inspector explains animation tracks (easing kind / spring source)."""

import pytest

from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.motion.motion_commands import StaggerElementsCommand
from src.features.teaching_animation.motion.spring import SpringConfig
from src.features.teaching_animation.ui.scene_inspector import SceneInspector
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack


@pytest.fixture()
def scene(qapp):
    element = SceneElement(
        "card", "shape", transform=SceneTransform(10, 10, 120, 80),
        content={"kind": "rect"},
        local_animation_tracks=(
            KeyframeTrack("opacity", (Keyframe(0, 0.0), Keyframe(11, 1.0, InterpolationType.EASE_OUT))),
        ),
    )
    document = TeachingSceneDocument(
        scene_id="ui", schema_version=3, name="ui", width=640, height=360,
        fps_num=24, fps_den=1, duration_frames=90, background={"color": "#000000"},
        animations=(), elements=(element,),
    )
    # A compiled spring plan drives scale_x on the same element.
    return StaggerElementsCommand(
        element_ids=("card",), preset_id="pop", param_name="scale_x", duration=20,
        params={"spring_config": SpringConfig(mass=1.0, damping=8.0, stiffness=200.0)},
    ).apply(document)


def test_inspector_lists_track_easing_and_spring_source(qtbot, scene) -> None:
    inspector = SceneInspector()
    qtbot.addWidget(inspector)
    inspector.set_selection((scene.element_by_id("card"),), scene=scene)
    summary = inspector.animation_summary.text()
    assert "opacity" in summary and "ease_out" in summary
    assert "scale_x" in summary
    assert 'spring "pop"' in summary
    assert "settle @ f" in summary


def test_inspector_clears_summary_without_selection(qtbot, scene) -> None:
    inspector = SceneInspector()
    qtbot.addWidget(inspector)
    inspector.set_selection((), scene=scene)
    assert inspector.animation_summary.text() == ""
