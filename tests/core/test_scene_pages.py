"""First-class scene pages: model, migration, commands and html-layer sync.

Pages (artboards) partition the timeline; elements carry a ``page_id`` and
legacy scenes migrate onto a single default page.  The html layer stays the
rendering backend while :func:`sync_pages_with_html_layer` mirrors its
detected sections into ``scene.pages``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.teaching_animation.core.commands import (
    AddPageCommand,
    MoveElementsToPageCommand,
    RemovePageCommand,
    ReorderPageCommand,
    SetDurationCommand,
    SetSceneBackgroundCommand,
    UpdatePageCommand,
)
from src.features.teaching_animation.core.html_layer import FreeformHtmlMotionLayer
from src.features.teaching_animation.core.html_layer_commands import (
    SetHtmlLayerCommand,
    SetHtmlPageTimingCommand,
)
from src.features.teaching_animation.core.html_page_deck import (
    PageDeckTiming,
    timing_metadata,
)
from src.features.teaching_animation.core.hierarchy import (
    Hierarchy,
    read_hierarchy,
    write_hierarchy_into,
)
from src.features.teaching_animation.core.model import (
    SceneElement,
    ScenePage,
    TeachingSceneDocument,
)
from src.features.teaching_animation.core.pages import sync_pages_with_html_layer
from src.features.teaching_animation.core.validator import validate_scene_json
from src.shared.keyframe import Keyframe, KeyframeTrack


def _element(element_id: str, page_id: str = "") -> SceneElement:
    return SceneElement(element_id=element_id, type="text", content=element_id,
                        page_id=page_id)


def _scene(elements: tuple[SceneElement, ...] = (),
           pages: tuple[ScenePage, ...] = (),
           duration_frames: int = 120) -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="s1", schema_version=3, name="Scene", width=1920, height=1080,
        fps_num=24, fps_den=1, duration_frames=duration_frames,
        background={"color": "#111827"}, elements=elements, animations=(),
        pages=pages,
    )


def _deck_layer(sections: int = 3, durations: tuple[float, ...] = (2.0, 3.0, 5.0)) -> FreeformHtmlMotionLayer:
    html = "".join(
        f'<section data-topos-page="{index + 1}">page {index + 1}</section>'
        for index in range(sections)
    )
    timing = PageDeckTiming(sections, "custom", durations_seconds=durations)
    return FreeformHtmlMotionLayer(
        layer_id="html_x", sanitized_html=html,
        viewport_width=1920, viewport_height=1080,
        fps_num=24, fps_den=1, duration_frames=240,
        metadata=timing_metadata(timing),
    )


# ---------------------------------------------------------------------------
# model: normalization + migration
# ---------------------------------------------------------------------------

class TestPageNormalization:
    def test_scene_without_pages_gets_one_default_page(self) -> None:
        scene = _scene()
        assert scene.page_ids == ("page_1",)
        assert scene.pages[0].duration_frames == 120

    def test_legacy_elements_land_on_first_page(self) -> None:
        scene = _scene(elements=(_element("a"), _element("b", page_id="page_1")))
        assert all(element.page_id == "page_1" for element in scene.elements)

    def test_unknown_page_id_falls_back_to_first_page(self) -> None:
        scene = _scene(
            elements=(_element("a", page_id="ghost"),),
            pages=(ScenePage(page_id="p1"), ScenePage(page_id="p2")),
        )
        assert scene.element_by_id("a").page_id == "p1"

    def test_duplicate_page_ids_are_dropped(self) -> None:
        scene = _scene(pages=(ScenePage(page_id="p1"), ScenePage(page_id="p1"),
                              ScenePage(page_id="p2")))
        assert scene.page_ids == ("p1", "p2")

    def test_from_dict_without_pages_migrates(self) -> None:
        data = _scene(elements=(_element("a"),)).to_dict()
        stripped = {key: value for key, value in data.items() if key != "pages"}
        for element in stripped["elements"]:
            element.pop("page_id", None)
        scene = TeachingSceneDocument.from_dict(stripped)
        assert scene.page_ids == ("page_1",)
        assert scene.pages[0].duration_frames == scene.duration_frames
        assert scene.element_by_id("a").page_id == "page_1"

    def test_round_trip_preserves_pages_and_assignments(self) -> None:
        scene = _scene(
            elements=(_element("a", page_id="p2"),),
            pages=(ScenePage(page_id="p1", duration_frames=48),
                   ScenePage(page_id="p2", name="Board", duration_frames=72)),
        )
        restored = TeachingSceneDocument.from_dict(scene.to_dict())
        assert restored.pages == scene.pages
        assert restored.element_by_id("a").page_id == "p2"


# ---------------------------------------------------------------------------
# model: page helpers
# ---------------------------------------------------------------------------

class TestPageHelpers:
    def test_page_start_frames_are_cumulative(self) -> None:
        scene = _scene(pages=(ScenePage(page_id="p1", duration_frames=48),
                              ScenePage(page_id="p2", duration_frames=72)))
        assert scene.page_start_frames() == (0, 48)

    def test_page_index_for_frame_maps_and_absorbs_tail(self) -> None:
        scene = _scene(pages=(ScenePage(page_id="p1", duration_frames=48),
                              ScenePage(page_id="p2", duration_frames=72)))
        assert scene.page_index_for_frame(0) == 0
        assert scene.page_index_for_frame(47) == 0
        assert scene.page_index_for_frame(48) == 1
        assert scene.page_index_for_frame(500) == 1
        assert scene.page_index_for_frame(-3) == 0

    def test_elements_for_page_and_limited_to_page(self) -> None:
        scene = _scene(
            elements=(_element("a", page_id="p1"), _element("b", page_id="p2")),
            pages=(ScenePage(page_id="p1"), ScenePage(page_id="p2")),
        )
        assert [e.element_id for e in scene.elements_for_page("p2")] == ["b"]
        view = scene.limited_to_page("p2")
        assert [e.element_id for e in view.elements] == ["b"]
        assert view.pages == (scene.page_by_id("p2"),)
        with pytest.raises(ValueError):
            scene.limited_to_page("ghost")


# ---------------------------------------------------------------------------
# model: with_page_* updaters
# ---------------------------------------------------------------------------

class TestPageUpdaters:
    def test_add_update_remove_reorder(self) -> None:
        scene = _scene(pages=(ScenePage(page_id="p1", duration_frames=60),))
        scene = scene.with_page_added(ScenePage(page_id="p2", duration_frames=60))
        assert scene.page_ids == ("p1", "p2")
        scene = scene.with_page_updated(ScenePage(page_id="p1", duration_frames=30))
        assert scene.pages[0].duration_frames == 30
        scene = scene.with_pages_reordered("p2", 0)
        assert scene.page_ids == ("p2", "p1")
        with pytest.raises(ValueError):
            scene.with_page_added(ScenePage(page_id="p1"))
        with pytest.raises(ValueError):
            scene.with_page_removed("ghost")
        with pytest.raises(ValueError):
            _scene().with_page_removed("page_1")

    def test_remove_page_reassigns_elements_to_previous_page(self) -> None:
        scene = _scene(
            elements=(_element("a", page_id="p1"), _element("b", page_id="p2"),
                      _element("c", page_id="p3")),
            pages=(ScenePage(page_id="p1"), ScenePage(page_id="p2"),
                   ScenePage(page_id="p3")),
        )
        scene = scene.with_page_removed("p2")
        assert scene.page_ids == ("p1", "p3")
        assert scene.element_by_id("b").page_id == "p1"

    def test_remove_first_page_falls_forward(self) -> None:
        scene = _scene(
            elements=(_element("a", page_id="p1"),),
            pages=(ScenePage(page_id="p1"), ScenePage(page_id="p2")),
        )
        scene = scene.with_page_removed("p1")
        assert scene.element_by_id("a").page_id == "p2"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

class TestPageCommands:
    def test_add_page_extends_duration_and_undoes(self) -> None:
        scene = _scene(pages=(ScenePage(page_id="p1", duration_frames=60),))
        command = AddPageCommand(ScenePage(page_id="p2", duration_frames=60))
        result = command.apply(scene)
        assert result.duration_frames == 120
        assert result.page_ids == ("p1", "p2")
        assert command.undo(result) == scene

    def test_remove_page_command_restores_elements_and_duration(self) -> None:
        scene = _scene(
            elements=(_element("a", page_id="p2"),),
            pages=(ScenePage(page_id="p1", duration_frames=60),
                   ScenePage(page_id="p2", duration_frames=60)),
        )
        command = RemovePageCommand("p2")
        result = command.apply(scene)
        assert result.page_ids == ("p1",)
        assert result.duration_frames == 60
        assert result.element_by_id("a").page_id == "p1"
        assert command.undo(result) == scene

    def test_update_page_duration_reanchors_duration(self) -> None:
        track = KeyframeTrack("x", min_val=-1000.0, max_val=1000.0)
        track.add_keyframe(Keyframe(65, 10.0))  # p2-local frame 5
        element = replace(_element("animated", page_id="p2"),
                          local_animation_tracks=(track,))
        scene = _scene(elements=(element,), pages=(
            ScenePage(page_id="p1", duration_frames=60),
            ScenePage(page_id="p2", duration_frames=60),
        ))
        command = UpdatePageCommand("p1", {"duration_frames": 90, "name": "Intro"})
        result = command.apply(scene)
        assert result.page_start_frames() == (0, 90)
        assert result.duration_frames == 150
        assert result.page_by_id("p1").name == "Intro"
        assert result.element_by_id("animated").local_animation_tracks[0].keyframes[0].frame == 95
        assert command.undo(result) == scene
        with pytest.raises(ValueError):
            UpdatePageCommand("p1", {"nope": 1}).apply(scene)

    def test_reorder_page_command(self) -> None:
        track = KeyframeTrack("x", min_val=-1000.0, max_val=1000.0)
        track.add_keyframe(Keyframe(30, 10.0))
        scene = _scene(
            elements=(replace(_element("b", page_id="p2"),
                              local_animation_tracks=(track,)),),
            pages=(ScenePage(page_id="p1", duration_frames=24),
                   ScenePage(page_id="p2", duration_frames=24),
                   ScenePage(page_id="p3", duration_frames=24)),
        )
        command = ReorderPageCommand("p3", 0)
        result = command.apply(scene)
        assert result.page_ids == ("p3", "p1", "p2")
        assert result.element_by_id("b").local_animation_tracks[0].keyframes[0].frame == 54
        assert command.undo(result).page_ids == ("p1", "p2", "p3")

    def test_set_duration_resizes_last_page_and_clamps_current_frame(self) -> None:
        scene = _scene(pages=(ScenePage(page_id="p1", duration_frames=24),
                              ScenePage(page_id="p2", duration_frames=24)))
        command = SetDurationCommand(25)
        result = command.apply(scene)
        assert [page.duration_frames for page in result.pages] == [24, 1]
        assert result.duration_frames == 25
        assert command.undo(result) == scene

    def test_move_elements_moves_frame_children_together(self) -> None:
        scene = _scene(
            elements=(_element("frame", page_id="p1"), _element("child", page_id="p1"),
                      _element("grandchild", page_id="p2"), _element("bystander", page_id="p1")),
            pages=(ScenePage(page_id="p1"), ScenePage(page_id="p2")),
        )
        metadata = write_hierarchy_into(
            scene.metadata,
            Hierarchy(parents={"child": "frame", "grandchild": "child"}),
        )
        scene = replace(scene, metadata=metadata)
        command = MoveElementsToPageCommand(("frame",), "p2")
        result = command.apply(scene)
        assert result.element_by_id("frame").page_id == "p2"
        assert result.element_by_id("child").page_id == "p2"
        assert result.element_by_id("grandchild").page_id == "p2"
        assert result.element_by_id("bystander").page_id == "p1"
        assert command.undo(result) == scene
        with pytest.raises(ValueError):
            MoveElementsToPageCommand(("frame",), "ghost").apply(scene)
        with pytest.raises(ValueError):
            MoveElementsToPageCommand(("ghost",), "p2").apply(scene)

    def test_moving_child_without_parent_is_rejected_across_pages(self) -> None:
        scene = _scene(
            elements=(_element("parent", page_id="p1"), _element("child", page_id="p1")),
            pages=(ScenePage(page_id="p1"), ScenePage(page_id="p2")),
        )
        scene = replace(scene, metadata=write_hierarchy_into(
            scene.metadata, Hierarchy(parents={"child": "parent"})))
        with pytest.raises(ValueError, match="parent element"):
            MoveElementsToPageCommand(("child",), "p2").apply(scene)

    def test_page_validation_catches_bad_span_and_cross_page_hierarchy(self) -> None:
        scene = _scene(
            elements=(_element("parent", page_id="p1"), _element("child", page_id="p2")),
            pages=(ScenePage(page_id="p1", duration_frames=60),
                   ScenePage(page_id="p2", duration_frames=60)),
        )
        data = scene.to_dict()
        data["pages"][1]["duration_frames"] = 59
        data["metadata"] = write_hierarchy_into(
            scene.metadata, Hierarchy(parents={"child": "parent"}),
        )
        result = validate_scene_json(data)
        codes = {issue.code for issue in result.issues}
        assert "duration" in codes
        assert "cross_page" in codes


# ---------------------------------------------------------------------------
# html-layer sync
# ---------------------------------------------------------------------------

class TestHtmlPageSync:
    def _html_scene(self, layer: FreeformHtmlMotionLayer | None = None,
                    elements: tuple[SceneElement, ...] = ()) -> TeachingSceneDocument:
        return replace(_scene(elements=elements), html_layer=layer or _deck_layer(),
                       duration_frames=240)

    def test_sync_builds_pages_from_sections(self) -> None:
        synced = sync_pages_with_html_layer(self._html_scene())
        assert len(synced.pages) == 3
        assert synced.page_start_frames() == (0, 48, 120)
        assert [page.duration_frames for page in synced.pages] == [48, 72, 120]

    def test_sync_preserves_ids_and_names_across_resync(self) -> None:
        synced = sync_pages_with_html_layer(self._html_scene())
        renamed = synced.with_page_updated(replace(synced.pages[0], name="封面"))
        resynced = sync_pages_with_html_layer(renamed)
        assert resynced.page_ids == synced.page_ids
        assert resynced.pages[0].name == "封面"

    def test_sync_count_shrink_moves_elements_to_last_page(self) -> None:
        synced = sync_pages_with_html_layer(self._html_scene())
        assigned = synced.with_element_added(
            _element("overlay", synced.page_ids[2]))
        two_sections = _deck_layer(sections=2, durations=(2.0, 8.0))
        shrunk = sync_pages_with_html_layer(
            replace(assigned, html_layer=two_sections))
        assert len(shrunk.pages) == 2
        assert shrunk.element_by_id("overlay").page_id == shrunk.page_ids[1]

    def test_sync_without_layer_is_noop(self) -> None:
        scene = _scene(pages=(ScenePage(page_id="p1"), ScenePage(page_id="p2")))
        assert sync_pages_with_html_layer(scene) is scene

    def test_legacy_html_file_without_pages_derives_sections(self) -> None:
        data = replace(_scene(), html_layer=_deck_layer(),
                       duration_frames=240).to_dict()
        stripped = {key: value for key, value in data.items() if key != "pages"}
        scene = TeachingSceneDocument.from_dict(stripped)
        assert len(scene.pages) == 3
        assert scene.page_start_frames() == (0, 48, 120)

    def test_set_html_layer_command_syncs_and_undoes(self) -> None:
        scene = _scene(elements=(_element("a", "page_1"),))
        command = SetHtmlLayerCommand(_deck_layer())
        result = command.apply(scene)
        assert len(result.pages) == 3
        assert result.duration_frames == 120  # scene duration untouched by sync
        assert command.undo(result) == scene

    def test_set_html_page_timing_updates_page_durations(self) -> None:
        scene = self._html_scene()
        command = SetHtmlPageTimingCommand(
            PageDeckTiming(3, "custom", durations_seconds=(4.0, 4.0, 2.0)))
        result = command.apply(scene)
        assert result.page_start_frames() == (0, 96, 192)
        assert [page.duration_frames for page in result.pages] == [96, 96, 48]
        assert command.undo(result) == scene

    def test_page_background_overrides_global_background_and_undoes(self) -> None:
        scene = _scene(pages=(
            ScenePage(page_id="p1", duration_frames=48, background="#ff0000"),
            ScenePage(page_id="p2", duration_frames=72),
        ))
        command = SetSceneBackgroundCommand({"color": "#102030"})
        updated = command.apply(scene)
        assert updated.background == {"color": "#102030"}
        assert updated.limited_to_page("p1").background == {"color": "#ff0000"}
        assert updated.limited_to_page("p2").background == {"color": "#102030"}
        assert command.undo(updated) == scene


# ---------------------------------------------------------------------------
# controller-facing page state
# ---------------------------------------------------------------------------

class TestControllerPageState:
    def test_current_page_id_follows_frame(self) -> None:
        from src.features.teaching_animation.ui.editor_controller import SceneEditorController

        scene = _scene(
            elements=(_element("a", "p1"),),
            pages=(ScenePage(page_id="p1", duration_frames=48),
                   ScenePage(page_id="p2", duration_frames=72)),
        )
        controller = SceneEditorController(scene)
        assert controller.current_page_id == "p1"
        controller.set_frame(60)
        assert controller.current_page_id == "p2"

    def test_render_page_filter_limits_elements(self) -> None:
        # limited_to_page is the render/hit-test view; exercised via the model
        # here because the offscreen SVG renderer is covered elsewhere.
        scene = _scene(
            elements=(_element("a", "p1"), _element("b", "p2")),
            pages=(ScenePage(page_id="p1"), ScenePage(page_id="p2")),
        )
        assert scene.limited_to_page("p1").element_by_id("b") is None
