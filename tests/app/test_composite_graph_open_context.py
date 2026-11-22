from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
from PyQt6.QtGui import QDropEvent

from src.app.controllers.composite_controller import CompositeControllerMixin
from src.app.main_window import PageContextManager, PageType
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, Rational, TimelineSequence
from src.features.composite.core.media_drop_payload import MIME_TYPE_MEDIA_POOL_ASSETS
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_panel import KeyframesPanel


class _FakeCompositePage:
    def __init__(self) -> None:
        self.loaded_data = None
        self.context_calls = []

    def load_composite_data(self, data) -> None:
        self.loaded_data = data

    def set_timeline_clip_context(self, runtime_context=None, gpu_provider=None) -> None:
        self.context_calls.append((runtime_context, gpu_provider))


class _Controller(CompositeControllerMixin):
    def __init__(self, project: EditProject, timeline: TimelineSequence, playhead: int) -> None:
        self._current_edit_project = project
        self._current_timeline_sequences = {timeline.timeline_id: timeline}
        self._page_context_manager = PageContextManager()
        self._composite_available = True
        self._current_composite_id = ""
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self.timeline_panel = SimpleNamespace(get_playhead_frame=lambda: playhead)
        self.composite_page = _FakeCompositePage()
        self.switched_context = None
        self.switched_page = None

    def switch_to_page_with_context(self, context) -> None:
        self.switched_context = context

    def switch_to_page(self, page_name: str) -> None:
        self.switched_page = page_name


def _project_with_timeline_clip() -> tuple[EditProject, TimelineSequence, ClipInstance]:
    project = EditProject.create("Composite Context Test")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "total_frames": 200,
            "duration_seconds": 200 / 24.0,
        },
        display_name="source.mov",
    )
    asset_id = next(asset.asset_id for asset in project.assets.values() if asset.path == "/tmp/source.mov")

    timeline = TimelineSequence(
        timeline_id="tl-context",
        name="Timeline",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = timeline.with_track_added(track)
    clip = ClipInstance(
        clip_id="clip-context",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=100,
        timeline_duration=40,
        source_start=50,
        source_duration=40,
        color_stack_id="stack-context",
    )
    timeline = timeline.with_clip_added("v1", clip)
    return project, timeline, clip


def test_timeline_backed_graph_open_uses_graph_clip_context() -> None:
    project, timeline, clip = _project_with_timeline_clip()
    project, _comp_clip_id, _comp_asset_id, graph_id = project.create_comp_clip_from_timeline_clip(
        timeline_id=timeline.timeline_id,
        clip_id=clip.clip_id,
        timeline=timeline,
    )

    controller = _Controller(project, timeline, playhead=123)
    controller._switch_to_composite_page(graph_id)

    context = controller.switched_context
    assert context is not None
    assert context.target_page == PageType.COMPOSITE
    assert context.composite_id == graph_id
    assert context.timeline_id == timeline.timeline_id
    assert context.clip_id == clip.clip_id
    assert context.asset_id == clip.asset_id
    assert context.timeline_frame == 123
    assert context.clip_local_frame == 23
    assert context.source_frame == 73
    assert context.color_stack_id == "stack-context"
    assert controller.switched_page is None


def test_timeline_comp_clip_names_are_project_unique_and_numbered() -> None:
    project, timeline, clip = _project_with_timeline_clip()

    project, _first_comp_id, _first_asset_id, first_graph_id = (
        project.create_comp_clip_from_timeline_clip(
            timeline_id=timeline.timeline_id,
            clip_id=clip.clip_id,
            timeline=timeline,
        )
    )
    first_graph = project.compositions[first_graph_id]
    assert first_graph["name"] == "Comp Clip 1"

    project, _second_comp_id, _second_asset_id, second_graph_id = (
        project.create_comp_clip_from_timeline_clip(
            timeline_id=timeline.timeline_id,
            clip_id=clip.clip_id,
            timeline=timeline,
        )
    )
    second_graph = project.compositions[second_graph_id]
    assert second_graph["name"] == "Comp Clip 2"


@pytest.mark.parametrize("target", ["node", "layer", "timeline"])
def test_initial_media_drop_creates_one_persisted_composition(qapp, target) -> None:
    project, timeline, clip = _project_with_timeline_clip()
    project, _, _, _ = project.create_empty_comp_clip(name="comp clip 1")
    project = project.import_asset(path="/tmp/card.png", media_type="image", metadata={}, display_name="Card")
    image_id = next(asset.asset_id for asset in project.assets.values() if asset.path == "/tmp/card.png")
    project = project.import_asset(path="/tmp/action.mts", media_type="video",
                                   metadata={"fps": 60, "total_frames": 600}, display_name="Action")
    action_id = next(asset.asset_id for asset in project.assets.values() if asset.path == "/tmp/action.mts")
    host = _Controller(project, timeline, playhead=0)
    page = CompositePage()
    panel = KeyframesPanel()
    host.composite_page = page
    page.set_keyframes_panel(panel)
    page.set_edit_project(project)
    page.set_initial_media_drop_handler(host._create_comp_clip_from_media_drop)
    page.reset_to_empty_workspace()
    try:
        mime = QMimeData()
        mime.setData(MIME_TYPE_MEDIA_POOL_ASSETS, json.dumps([clip.asset_id, image_id, action_id]).encode())
        drop = QDropEvent(QPointF(25, 75), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        views = {"node": page._node_editor, "layer": panel._tree_view, "timeline": panel._timeline_view}
        views[target].dropEvent(drop)
        assert drop.isAccepted() and page.has_active_composition()
        assert len(host._current_edit_project.comp_clips) == 2
        saved = host._current_edit_project.compositions[host._current_composite_id]
        assert saved["name"] == "Comp Clip 2"
        assert saved["fps"] == 24 and saved["duration_frames"] == 600
        assert saved["width"] == 1920 and saved["height"] == 1080
        assert saved["comp_clip_id"] == host._current_edit_project.active_comp_clip_id
        assert host._current_edit_project.get_comp_clip_by_graph_id(host._current_composite_id).comp_clip_id == saved["comp_clip_id"]
        assert len(saved["layers"]) == 4  # Three media sources and the transparent background.
        assert {node["type"] for node in saved["nodes"]} >= {"video_frame", "image_input", "merge", "output"}
        assert host.edit_project_dirty
        page.undo()
        assert not any(node.node_type in ("video_frame", "image_input") for node in page.node_graph.nodes.values())
        assert len(page.node_graph.get_all_layers()) == 1
        host._sync_composite_graph_to_edit_project(host._current_composite_id, page.get_composite_data())
        assert len(host._current_edit_project.compositions[host._current_composite_id]["layers"]) == 1
        page.redo()
        assert sum(node.node_type in ("video_frame", "image_input") for node in page.node_graph.nodes.values()) == 3
        assert len(page.node_graph.get_all_layers()) == 4
    finally:
        page.close()
        panel.close()


def test_initial_drop_does_not_publish_a_partial_composition(qapp, monkeypatch) -> None:
    project, timeline, _ = _project_with_timeline_clip()
    host = _Controller(project, timeline, playhead=0)
    page = CompositePage()
    host.composite_page = page
    page.set_initial_media_drop_handler(host._create_comp_clip_from_media_drop)
    page.reset_to_empty_workspace()
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("/tmp/notes.txt"), QUrl.fromLocalFile("/tmp/unsupported.zip")])
        assert not page._handle_initial_media_drop(mime)
        mime.setUrls([QUrl.fromLocalFile("/tmp/source.mov"), QUrl.fromLocalFile("/tmp/card.png")])
        add_layer = page._add_dropped_media_layer

        def fail_second(controller, payload, index: int) -> None:
            if index == 1:
                raise ValueError("Invalid second layer")
            add_layer(controller, payload, index)

        monkeypatch.setattr(page, "_add_dropped_media_layer", fail_second)
        assert not page._handle_initial_media_drop(mime)
        assert host._current_edit_project is project
        assert not page.has_active_composition()
        assert host.switched_page is None
    finally:
        page.close()


def test_audio_drop_creates_registered_layer_and_waveform(qtbot, tmp_path) -> None:
    import wave

    path = tmp_path / "voice.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(b"\x11\x11" * 4800)
    project, timeline, _ = _project_with_timeline_clip()
    host = _Controller(project, timeline, playhead=0)
    page = CompositePage()
    panel = KeyframesPanel()
    qtbot.addWidget(page)
    qtbot.addWidget(panel)
    host.composite_page = page
    page.set_edit_project(project)
    page.set_keyframes_panel(panel)
    page.set_initial_media_drop_handler(host._create_comp_clip_from_media_drop)
    page.reset_to_empty_workspace()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    assert page._handle_initial_media_drop(mime)
    qtbot.waitUntil(page.has_active_composition)
    saved = host._current_edit_project.compositions[host._current_composite_id]
    asset = host._current_edit_project.get_asset_by_path(str(path))
    assert asset is not None
    layer = next(layer for layer in saved["layers"].values() if layer["layer_type"] == "audio")
    assert layer["audio"]["asset_id"] == asset.asset_id
    assert saved["duration_frames"] == 3
    qtbot.waitUntil(lambda: layer["layer_id"] in getattr(panel._timeline_view, "_audio_waveforms", {}))
    panel._on_layer_eye_changed(layer["root_node_id"], False)
    assert page.node_graph.get_layer(layer["layer_id"]).audio.mute
    page.undo()
    assert not page.node_graph.get_layer(layer["layer_id"]).audio.mute
    page.undo()
    assert not any(item.audio for item in page.node_graph.get_all_layers())
    page.redo()
    assert any(item.audio for item in page.node_graph.get_all_layers())


def test_photoshop_drop_adds_separate_layers_in_one_transaction(qtbot, tmp_path) -> None:
    psd_tools = pytest.importorskip("psd_tools")
    from PIL import Image
    from psd_tools.api.layers import PixelLayer

    original = tmp_path / "original"
    original.mkdir()
    source = original / "stack.psd"
    document = psd_tools.PSDImage.new("RGBA", (8, 8))
    PixelLayer.frompil(Image.new("RGBA", (2, 2), (255, 0, 0, 128)), document,
                       name="Same name", left=-1, top=2)
    hidden = PixelLayer.frompil(Image.new("RGBA", (2, 2), (0, 255, 0, 255)), document,
                                name="Same name", left=4, top=4)
    hidden.visible = False
    document.save(source)
    project, timeline, _ = _project_with_timeline_clip()
    host = _Controller(project, timeline, playhead=0)
    page = CompositePage()
    qtbot.addWidget(page)
    host.composite_page = page
    page.set_initial_media_drop_handler(host._create_comp_clip_from_media_drop)
    page.reset_to_empty_workspace()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source))])

    assert page._handle_initial_media_drop(mime)
    qtbot.waitUntil(page.has_active_composition)
    graph = page.node_graph
    stack = graph.get_main_layer_stack()
    imported = [graph.get_layer(layer_id) for layer_id in stack.layer_ids[:2]]
    assert [layer.name for layer in imported] == ["Same name", "Same name"]
    assert [layer.enabled for layer in imported] == [False, True]
    sources = [graph.get_node(layer.source_node_id) for layer in imported]
    assert len({tuple(node.parameters["psd_layer"]["selector"]) for node in sources}) == 2
    asset = host._current_edit_project.get_asset_by_path(str(source))
    assert {node.parameters["asset_id"] for node in sources} == {asset.asset_id}
    page.undo()
    assert not any(node.node_type == "image_input" for node in page.node_graph.nodes.values())
    page.redo()
    assert len([node for node in page.node_graph.nodes.values() if node.node_type == "image_input"]) == 2

    from src.shared.project.topos_project import ToposProject
    from src.shared.project.topos_project_io import ToposProjectIO

    graph_id = host._current_composite_id
    archive = original / "stack.topos"
    package = ToposProject.create_single_asset("PSD", str(source), "image")
    assert ToposProjectIO.save(package, str(archive), full_compositions_data={graph_id: graph.to_dict()})
    moved = tmp_path / "moved"
    original.rename(moved)
    _, modules = ToposProjectIO.load(str(moved / archive.name), load_full_data=True)
    assert modules is not None
    reopened = modules["compositions"][graph_id]
    restored = [node["parameters"] for node in reopened["nodes"] if node["type"] == "image_input"]
    assert {node["file_path"] for node in restored} == {str(moved / source.name)}
    assert {tuple(node["psd_layer"]["selector"]) for node in restored} == {
        tuple(node.parameters["psd_layer"]["selector"]) for node in sources
    }


def test_corrupt_photoshop_drop_leaves_initial_composition_unpublished(qtbot, tmp_path) -> None:
    pytest.importorskip("psd_tools")
    corrupt = tmp_path / "broken.psd"
    corrupt.write_bytes(b"truncated")
    project, timeline, _ = _project_with_timeline_clip()
    host = _Controller(project, timeline, playhead=0)
    page = CompositePage()
    qtbot.addWidget(page)
    host.composite_page = page
    page.set_initial_media_drop_handler(host._create_comp_clip_from_media_drop)
    page.reset_to_empty_workspace()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("/tmp/card.png"), QUrl.fromLocalFile(str(corrupt))])
    messages = []
    page.status_message.connect(messages.append)

    assert page._handle_initial_media_drop(mime)
    qtbot.waitUntil(lambda: bool(messages))
    assert not page.has_active_composition()
    assert host._current_edit_project is project


def test_timeline_context_uses_live_clip_range_and_timeline_format() -> None:
    project, timeline, clip = _project_with_timeline_clip()
    project, _comp_clip_id, _comp_asset_id, graph_id = project.create_comp_clip_from_timeline_clip(
        timeline_id=timeline.timeline_id,
        clip_id=clip.clip_id,
        timeline=timeline,
    )
    # Simulate a graph saved before the Edit clip was trimmed. The current
    # timeline segment must win when building the runtime context.
    project.compositions[graph_id]["duration_frames"] = 999

    controller = _Controller(project, timeline, playhead=100)
    controller._current_project_id = project.project_id
    controller.engine = SimpleNamespace()
    controller._timeline_clip_frame_provider = None
    context = controller._page_context_manager.create_context_for_edit_to_composite(
        clip_id=clip.clip_id,
        composite_id=graph_id,
        asset_id=clip.asset_id,
        timeline_id=timeline.timeline_id,
        timeline_frame=clip.timeline_start,
        source_frame=clip.source_start,
        clip_local_frame=0,
    )

    controller._setup_timeline_clip_context(context, graph_id, clip.clip_id)

    runtime, _provider = controller.composite_page.context_calls[-1]
    assert runtime.width == timeline.width
    assert runtime.height == timeline.height
    assert runtime.fps == 24.0
    assert runtime.duration_frames == clip.timeline_duration


def test_timeline_backed_graph_open_clamps_playhead_to_clip_start() -> None:
    project, timeline, clip = _project_with_timeline_clip()
    project, _comp_clip_id, _comp_asset_id, graph_id = project.create_comp_clip_from_timeline_clip(
        timeline_id=timeline.timeline_id,
        clip_id=clip.clip_id,
        timeline=timeline,
    )

    controller = _Controller(project, timeline, playhead=500)
    controller._switch_to_composite_page(graph_id)

    context = controller.switched_context
    assert context.timeline_frame == clip.timeline_start
    assert context.clip_local_frame == 0
    assert context.source_frame == clip.source_start


def test_empty_comp_clip_open_clears_stale_timeline_context() -> None:
    project = EditProject.create("Empty Comp Context Test")
    project, comp_clip_id, _comp_asset_id, graph_id = project.create_empty_comp_clip()
    timeline = TimelineSequence(timeline_id="tl-empty", name="Timeline")

    controller = _Controller(project, timeline, playhead=0)
    controller._switch_to_composite_page(graph_id)

    assert project.active_comp_clip_id == comp_clip_id
    assert controller.switched_context is None
    assert controller.switched_page == "Composite"
    assert controller.composite_page.loaded_data == project.get_composition_by_id(graph_id)
    assert controller.composite_page.context_calls == [(None, None)]


def test_empty_comp_clip_open_does_not_leak_auto_bind_suppress_flag() -> None:
    """Opening a graph while already on Composite must not leak the one-shot
    auto-bind suppress flag: switch_to_page early-returns there, so nobody
    consumes it, and a stale True makes the next tab return skip workspace
    restore and reset the open composition."""
    project = EditProject.create("Suppress Flag Leak Test")
    project, _comp_clip_id, _comp_asset_id, graph_id = project.create_empty_comp_clip()
    timeline = TimelineSequence(timeline_id="tl-suppress", name="Timeline")

    controller = _Controller(project, timeline, playhead=0)
    controller._switch_to_composite_page(graph_id)

    assert controller.switched_page == "Composite"
    assert controller._suppress_composite_auto_bind_once is False


def test_legacy_graph_open_restores_missing_composite_identity() -> None:
    """Graphs keyed by ID still activate Composite when their payload is old."""
    project = EditProject.create("Legacy Composite Open Test")
    project, comp_clip_id, asset_id, graph_id = project.create_empty_comp_clip()
    legacy_graph = dict(project.compositions[graph_id])
    legacy_graph.pop("composite_id", None)
    legacy_graph.pop("comp_clip_id", None)
    legacy_graph.pop("comp_asset_id", None)
    legacy_graph.pop("source_kind", None)
    project = replace(project, compositions={graph_id: legacy_graph})
    timeline = TimelineSequence(timeline_id="tl-legacy", name="Timeline")

    controller = _Controller(project, timeline, playhead=0)
    controller._switch_to_composite_page(graph_id)

    assert controller.composite_page.loaded_data["composite_id"] == graph_id
    assert controller.composite_page.loaded_data["comp_clip_id"] == comp_clip_id
    assert controller.composite_page.loaded_data["comp_asset_id"] == asset_id
    assert controller.composite_page.loaded_data["source_kind"] == "empty_comp_clip"


def test_unchanged_composite_graph_does_not_rewrite_edit_project() -> None:
    """Playback graph evaluation must not create a new project revision."""
    project, timeline, clip = _project_with_timeline_clip()
    project, _comp_clip_id, _comp_asset_id, graph_id = project.create_comp_clip_from_timeline_clip(
        timeline_id=timeline.timeline_id,
        clip_id=clip.clip_id,
        timeline=timeline,
    )
    controller = _Controller(project, timeline, playhead=clip.timeline_start)
    graph_data = project.compositions[graph_id]

    controller._sync_composite_graph_to_edit_project(graph_id, graph_data)

    assert controller._current_edit_project is project
