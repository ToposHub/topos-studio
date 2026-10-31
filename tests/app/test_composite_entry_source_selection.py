from __future__ import annotations

from types import SimpleNamespace

from src.app.controllers.composite_controller import CompositeControllerMixin
from src.app.controllers.page_controller import PageManagementMixin
from src.app.main_window import PageContextManager
from src.features.composite.render.render_scheduler import RenderMode, RequestIdGenerator
from src.features.composite.ui.composite_page import CompositePage
from src.features.edit.core.timeline_v2 import ClipInstance

from tests.app.test_composite_graph_open_context import _project_with_timeline_clip


class _FakeCompositePage:
    def __init__(self, has_timeline: bool = False) -> None:
        self._has_timeline = has_timeline
        self.timeline_switches = []
        self.ensure_calls = []
        self.context_calls = []
        self.project_calls = []
        self.color_output_switches = 0
        self._node_graph = SimpleNamespace(nodes={})

    def requires_color_output_input(self) -> bool:
        return False

    def has_timeline_clip_input(self) -> bool:
        return self._has_timeline

    def switch_to_timeline_input_graph(self, color_stack_id: str = "") -> bool:
        self._has_timeline = True
        self.timeline_switches.append(color_stack_id)
        return True

    def ensure_topos_color_node(self, color_stack_id: str = "") -> bool:
        self.ensure_calls.append(color_stack_id)
        return True

    def set_timeline_clip_context(self, runtime_context=None, gpu_provider=None) -> None:
        self.context_calls.append((runtime_context, gpu_provider))

    def set_edit_project(self, project=None) -> None:
        self.project_calls.append(project)

    def switch_to_color_output_graph(self) -> bool:
        self.color_output_switches += 1
        return True


class _EntryController(PageManagementMixin):
    def __init__(self) -> None:
        self._current_edit_project = object()
        self._composite_available = True
        self.feature_registry = None
        self.composite_page = _FakeCompositePage()
        self._page_context_manager = SimpleNamespace(get_current_context=lambda: None)
        self._contextual_page_switch_in_progress = False
        self._page_switch_from_page = "Edit"
        self.timeline_panel = None
        self._composite_context_applied_during_enter = False
        self._composite_adapter = SimpleNamespace(on_enter_page=lambda *args: None)
        self.engine = SimpleNamespace(
            color_output=SimpleNamespace(
                is_valid=False,
                is_dirty=False,
                texture_id=0,
                width=0,
                height=0,
                revision=0,
            ),
            request_color_output=lambda: None,
        )
        self.standalone_calls = 0
        self.autobind_calls = 0

    def _try_standalone_image_composite(self) -> bool:
        self.standalone_calls += 1
        return True

    def _auto_bind_composite_to_selected_clip(self) -> bool:
        self.autobind_calls += 1
        return True


class _RememberedCompositePage:
    def __init__(self) -> None:
        self._current_composite_id = "graph-1"
        self._runtime_context = None
        self.loaded_data = None
        self.project_calls = []
        self.context_calls = []
        self.active_calls = []

    def set_edit_project(self, project=None) -> None:
        self.project_calls.append(project)

    def requires_color_output_input(self) -> bool:
        return False

    def load_composite_data(self, data) -> None:
        self.loaded_data = data
        self._current_composite_id = data["composite_id"]

    def set_timeline_clip_context(self, runtime_context=None, gpu_provider=None) -> None:
        self.context_calls.append((runtime_context, gpu_provider))

    def set_composition_active(self, active: bool) -> None:
        self.active_calls.append(active)


class _RememberedWorkspaceController(PageManagementMixin):
    def __init__(self) -> None:
        composition = {
            "composite_id": "graph-1",
            "nodes": [],
            "connections": [],
        }
        self._current_edit_project = SimpleNamespace(
            get_composition_by_id=lambda composite_id: (
                composition if composite_id == "graph-1" else None
            ),
        )
        self._current_composite_id = "graph-1"
        self._composite_available = True
        self.feature_registry = None
        self.composite_page = _RememberedCompositePage()
        self._page_context_manager = SimpleNamespace(
            get_current_context=lambda: None,
        )
        self._contextual_page_switch_in_progress = False
        self._suppress_composite_auto_bind_once = False
        self._page_switch_from_page = "Color"
        self.engine = SimpleNamespace(
            color_output=SimpleNamespace(
                is_valid=False,
                is_dirty=False,
                texture_id=0,
                width=0,
                height=0,
                revision=0,
            ),
            request_color_output=lambda: None,
            img_full_original=None,
        )
        self._composite_adapter = SimpleNamespace(
            on_enter_page=lambda *args: None,
        )
        self._set_composite_playback_frame_sync = lambda enabled: None
        self._configure_composite_timeline_playback_controls = lambda enabled: None
        self._configure_composite_comp_clip_controls = lambda: None


def test_switching_back_to_composite_restores_remembered_workspace() -> None:
    """Returning from another page must not fall through to empty workspace."""
    controller = _RememberedWorkspaceController()

    controller.enter_page_hook("Composite")

    assert controller.composite_page.loaded_data["composite_id"] == "graph-1"
    assert controller.composite_page.active_calls == [True]
    assert controller.composite_page.project_calls == [controller._current_edit_project]
    assert controller._composite_context_applied_during_enter is True


def test_leaving_composite_clears_stale_auto_bind_suppress_flag() -> None:
    """The one-shot suppress flag must never survive leaving Composite.

    It is raised right before switching INTO Composite; when that switch
    early-returns (user already on the page) nobody consumes it, and a
    stale True makes the next tab return skip workspace restore and reset
    the open composition."""
    controller = _RememberedWorkspaceController()
    controller._suppress_composite_auto_bind_once = True
    controller._composite_adapter = SimpleNamespace(
        on_enter_page=lambda *args: None,
        on_leave_page=lambda: None,
    )

    controller.leave_page_hook("Composite")

    assert controller._suppress_composite_auto_bind_once is False

    controller.enter_page_hook("Composite")
    assert controller.composite_page.loaded_data["composite_id"] == "graph-1"
    assert controller._composite_context_applied_during_enter is True


class _EditReturnController(_RememberedWorkspaceController):
    """Edit -> Composite return with no bindable selected clip."""

    def __init__(self) -> None:
        super().__init__()
        self._page_switch_from_page = "Edit"
        self._edit_available = False
        self.timeline_panel = None


def test_edit_return_restores_remembered_workspace_when_auto_bind_declines() -> None:
    """Falling back from declined auto-bind must restore, not reset."""
    controller = _EditReturnController()

    controller.enter_page_hook("Composite")

    assert controller.composite_page.loaded_data["composite_id"] == "graph-1"
    assert controller.composite_page.active_calls == [True]
    assert controller._composite_context_applied_during_enter is True


class _StandaloneImageController(PageManagementMixin):
    def __init__(self) -> None:
        self.current_file_path = "/tmp/image.png"
        self.current_file_is_video = False
        self.composite_page = _FakeCompositePage()
        self._color_session_manager = SimpleNamespace(
            get_current_session=lambda: SimpleNamespace(is_standalone_image=lambda: True)
        )


class _TimelineContextController(CompositeControllerMixin):
    def __init__(self) -> None:
        project, timeline, clip = _project_with_timeline_clip()
        self._current_edit_project = project
        self._current_timeline_sequences = {timeline.timeline_id: timeline}
        self._page_context_manager = PageContextManager()
        self._current_composite_id = ""
        self._current_project_id = project.project_id
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self.composite_page = _FakeCompositePage(has_timeline=False)
        self.engine = SimpleNamespace()
        self._timeline_clip_frame_provider = None
        self.timeline = timeline
        self.clip = clip


class _ColorSessionCompositeController(PageManagementMixin, CompositeControllerMixin):
    def __init__(self) -> None:
        project, timeline, clip = _project_with_timeline_clip()
        # Color → Composite is allowed here because this fixture represents
        # the explicit "Create Comp Clip" path and already has a graph id.
        clip = clip.with_composite_id("graph-1")
        timeline = timeline.with_clip_updated(clip)
        self._current_edit_project = project
        self._current_timeline_sequences = {timeline.timeline_id: timeline}
        self._page_context_manager = PageContextManager()
        self._current_composite_id = ""
        self._current_project_id = project.project_id
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self.timeline_panel = None
        self.composite_page = _FakeCompositePage(has_timeline=False)
        self.engine = SimpleNamespace()
        self._timeline_clip_frame_provider = None
        self.current_file_is_video = True
        self.video_controller = SimpleNamespace(current_frame=65)
        self._color_session_manager = SimpleNamespace(
            get_current_session=lambda: SimpleNamespace(
                is_timeline_clip=lambda: True,
                clip_id=clip.clip_id,
                timeline_id=timeline.timeline_id,
                asset_id=clip.asset_id,
                color_stack_id=clip.color_stack_id,
                timeline_frame=None,
                source_frame=clip.source_start,
            )
        )
        self.timeline = timeline
        self.clip = clip

    def _ensure_clip_has_color_stack(self, clip_id: str, asset_id: str) -> str:
        return "created-stack"


def test_edit_to_composite_does_not_use_stale_standalone_image_branch() -> None:
    controller = _EntryController()

    controller.enter_page_hook("Composite")

    assert controller.standalone_calls == 0
    assert controller.autobind_calls == 1
    assert controller.composite_page.project_calls == [controller._current_edit_project]


def test_standalone_image_uses_color_output_graph() -> None:
    controller = _StandaloneImageController()

    assert controller._try_standalone_image_composite() is True

    assert controller.composite_page.color_output_switches == 1


def test_edit_context_replaces_stale_image_graph_with_timeline_graph() -> None:
    controller = _TimelineContextController()
    context = controller._page_context_manager.create_context_for_edit_to_composite(
        clip_id=controller.clip.clip_id,
        composite_id="",
        asset_id=controller.clip.asset_id,
        timeline_id=controller.timeline.timeline_id,
        timeline_frame=controller.clip.timeline_start,
        source_frame=controller.clip.source_start,
        clip_local_frame=0,
        color_stack_id=controller.clip.color_stack_id,
    )

    controller._handle_enter_composite_with_context(context)

    assert controller.composite_page.timeline_switches == [controller.clip.color_stack_id]
    assert controller.composite_page.context_calls
    assert controller.composite_page.project_calls == [controller._current_edit_project]


def test_color_timeline_session_binds_video_composite_context() -> None:
    controller = _ColorSessionCompositeController()

    assert controller._auto_bind_composite_to_color_session() is True

    runtime_context = controller.composite_page.context_calls[-1][0]
    assert controller.composite_page.timeline_switches == [controller.clip.color_stack_id]
    assert runtime_context.clip_id == controller.clip.clip_id
    assert runtime_context.timeline_id == controller.timeline.timeline_id
    assert runtime_context.source_frame == 65
    assert runtime_context.timeline_frame == controller.clip.timeline_start + 15


class _FakeRenderer:
    def __init__(self) -> None:
        self.submissions = []

    def submit(self, command, data) -> None:
        self.submissions.append((command, data))


class _FakeGraph:
    def __init__(self) -> None:
        self._force_full_eval = False
        self._generation = 7

    @property
    def generation(self) -> int:
        # AUD-9508：CompositePage._build_render_request 经 NodeGraph 公开
        # generation 属性做重发布门控，桩需对齐该契约
        return self._generation

    def to_dict(self) -> dict:
        return {}

    def snapshot(self, **kwargs):
        return SimpleNamespace(generation=self._generation, **kwargs)


def _bind_request_builder(page) -> None:
    """对齐真实 CompositePage 的请求构建（P1-APP-01）。

    `_do_evaluate_graph` 现在经由 `_build_render_request` 构造不可变
    RenderRequest；fake page 需要绑定真实实现（viewer ROI 以 None 桩掉，
    viewer 本身不在被测路径上）。
    """
    page._build_render_request = lambda: CompositePage._build_render_request(page)
    page._viewer_user_roi_bbox = lambda: None
    page._refresh_asset_dependency_panel = lambda: None


def test_composite_evaluate_request_carries_edit_project() -> None:
    project = object()
    renderer = _FakeRenderer()
    page = SimpleNamespace(
        _renderer=renderer,
        _node_graph=_FakeGraph(),
        _current_composite_id="",
        _request_id_gen=RequestIdGenerator(),
        _last_color_texture_id=0,
        _last_color_width=0,
        _last_color_height=0,
        _last_color_revision=0,
        _current_render_mode=RenderMode.PREVIEW,
        _runtime_context=None,
        _timeline_clip_gpu_provider=None,
        _project=project,
        _is_active=True,
        _interaction_active=False,
        _current_keyframe_frame=lambda: 0,
        _apply_view_mode_overrides=lambda snapshot: None,
        # 对齐真实 CompositePage._build_playback_drive（_do_evaluate_graph 调用）
        _build_playback_drive=lambda snapshot: None,
        _resolve_preview_node_id=lambda: None,
        _view_signature=lambda: "",
    )
    _bind_request_builder(page)

    CompositePage._do_evaluate_graph(page)

    request = renderer.submissions[0][1]["request"]
    assert request.edit_project is project


def test_mark_color_dirty_forces_new_graph_generation() -> None:
    graph = _FakeGraph()
    page = SimpleNamespace(_color_dirty=False, _node_graph=graph)

    CompositePage.mark_color_dirty(page)

    assert page._color_dirty is True
    assert graph._force_full_eval is True
    assert graph._generation == 8


class _FakeTimelineNode:
    node_type = "timeline_clip_input"

    def __init__(self) -> None:
        self.cached_textures = []
        self.parameters = {}

    def set_cached_texture(self, texture) -> None:
        self.cached_textures.append(texture)

    def set_parameter(self, name: str, value) -> None:
        self.parameters[name] = value


class _FakeViewer:
    def __init__(self) -> None:
        self.textures = []

    def set_texture(self, tex_id: int, width: int, height: int) -> None:
        self.textures.append((tex_id, width, height))


def test_timeline_context_change_submits_current_request_id() -> None:
    renderer = _FakeRenderer()
    timeline_node = _FakeTimelineNode()
    graph = _FakeGraph()
    graph.nodes = {"src": timeline_node}
    page = SimpleNamespace(
        _runtime_context=None,
        _timeline_clip_gpu_provider=None,
        _last_submitted_request_id=0,
        _last_texture_id=99,
        _last_texture_width=16,
        _last_texture_height=16,
        _last_metadata={"old": True},
        _request_id_gen=RequestIdGenerator(),
        _viewer=_FakeViewer(),
        _is_active=True,
        _renderer=renderer,
        _node_graph=graph,
        _current_composite_id="",
        _last_color_texture_id=0,
        _last_color_width=0,
        _last_color_height=0,
        _last_color_revision=0,
        _current_render_mode=RenderMode.PREVIEW,
        _project=None,
        _interaction_active=False,
        _current_keyframe_frame=lambda: 0,
        _apply_view_mode_overrides=lambda snapshot: None,
        # 对齐真实 CompositePage._build_playback_drive（_do_evaluate_graph 调用）
        _build_playback_drive=lambda snapshot: None,
        _resolve_preview_node_id=lambda: None,
        _view_signature=lambda: "",
    )
    page._evaluate_graph = lambda: CompositePage._do_evaluate_graph(page)
    _bind_request_builder(page)
    runtime_context = SimpleNamespace(
        timeline_id="tl",
        clip_id="clip",
        source_frame=10,
        asset_id="asset",
    )

    CompositePage.set_timeline_clip_context(
        page,
        runtime_context=runtime_context,
        gpu_provider=SimpleNamespace(clear_source_cache=lambda: None),
    )

    request = renderer.submissions[-1][1]["request"]
    assert page._last_submitted_request_id == request.request_id
    assert request.cancel_on_supersede is False
    assert timeline_node.cached_textures == [None]
    assert timeline_node.parameters == {
        "timeline_id": "tl",
        "clip_id": "clip",
        "asset_id": "asset",
    }


def test_timeline_frame_advance_keeps_same_clip_source_cache() -> None:
    renderer = _FakeRenderer()
    timeline_node = _FakeTimelineNode()
    graph = _FakeGraph()
    graph.nodes = {"src": timeline_node}
    viewer = _FakeViewer()
    provider = SimpleNamespace(source_cache_clears=0)
    provider.clear_source_cache = lambda: setattr(
        provider, "source_cache_clears", provider.source_cache_clears + 1
    )
    page = SimpleNamespace(
        _runtime_context=None,
        _timeline_clip_gpu_provider=None,
        _last_submitted_request_id=0,
        _last_texture_id=99,
        _last_texture_width=16,
        _last_texture_height=16,
        _last_metadata={"old": True},
        _request_id_gen=RequestIdGenerator(),
        _viewer=viewer,
        _is_active=True,
        _renderer=renderer,
        _node_graph=graph,
        _current_composite_id="",
        _last_color_texture_id=0,
        _last_color_width=0,
        _last_color_height=0,
        _last_color_revision=0,
        _current_render_mode=RenderMode.PREVIEW,
        _project=None,
        _interaction_active=False,
        _current_keyframe_frame=lambda: 0,
        _apply_view_mode_overrides=lambda snapshot: None,
        # 对齐真实 CompositePage._build_playback_drive（_do_evaluate_graph 调用）
        _build_playback_drive=lambda snapshot: None,
        _resolve_preview_node_id=lambda: None,
        _view_signature=lambda: "",
    )
    page._evaluate_graph = lambda: CompositePage._do_evaluate_graph(page)
    _bind_request_builder(page)
    first = SimpleNamespace(
        timeline_id="tl",
        clip_id="clip",
        source_frame=10,
        asset_id="asset",
    )
    next_frame = SimpleNamespace(
        timeline_id="tl",
        clip_id="clip",
        source_frame=11,
        asset_id="asset",
    )

    CompositePage.set_timeline_clip_context(page, first, provider)
    page._last_texture_id = 123
    page._last_texture_width = 1920
    page._last_texture_height = 1080
    CompositePage.set_timeline_clip_context(page, next_frame, provider)

    assert provider.source_cache_clears == 1
    assert timeline_node.cached_textures == [None]
    assert viewer.textures == [(0, 0, 0)]
    assert page._last_texture_id == 123
    assert len(renderer.submissions) == 2
    assert all(
        submission[1]["request"].cancel_on_supersede is False
        for submission in renderer.submissions
    )


def test_composite_playback_does_not_publish_unchanged_graph_each_frame() -> None:
    renderer = _FakeRenderer()
    published = []
    page = SimpleNamespace(
        _renderer=renderer,
        _node_graph=_FakeGraph(),
        _current_composite_id="graph-1",
        _request_id_gen=RequestIdGenerator(),
        _last_color_texture_id=0,
        _last_color_width=0,
        _last_color_height=0,
        _last_color_revision=0,
        _current_render_mode=RenderMode.PREVIEW,
        _runtime_context=None,
        _timeline_clip_gpu_provider=None,
        _project=object(),
        _is_active=True,
        _interaction_active=False,
        _current_keyframe_frame=lambda: 0,
        _apply_view_mode_overrides=lambda snapshot: None,
        # 对齐真实 CompositePage._build_playback_drive（_do_evaluate_graph 调用）
        _build_playback_drive=lambda snapshot: None,
        _resolve_preview_node_id=lambda: None,
        _view_signature=lambda: "",
        composite_graph_changed=SimpleNamespace(
            emit=lambda composite_id, graph_data: published.append((composite_id, graph_data))
        ),
    )
    _bind_request_builder(page)

    CompositePage._do_evaluate_graph(page)
    CompositePage._do_evaluate_graph(page)

    assert published == [("graph-1", {})]
    assert len(renderer.submissions) == 2


class _FakeSignal:
    def __init__(self) -> None:
        self.slots = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def disconnect(self, slot) -> None:
        try:
            self.slots.remove(slot)
        except ValueError:
            raise TypeError("not connected")

    def emit(self, *args) -> None:
        for slot in list(self.slots):
            slot(*args)


class _FakeTransportBar:
    def __init__(self) -> None:
        self.seek_requested = _FakeSignal()
        self.scrub_started = _FakeSignal()
        self.scrub_moved = _FakeSignal()
        self.scrub_finished = _FakeSignal()
        self.play_toggled = _FakeSignal()
        self.next_frame = _FakeSignal()
        self.prev_frame = _FakeSignal()
        self.first_frame = _FakeSignal()
        self.last_frame = _FakeSignal()
        self.mark_in_requested = _FakeSignal()
        self.mark_out_requested = _FakeSignal()
        self.clear_in_out_requested = _FakeSignal()
        self.visible = False
        self.io_visible = False
        self.total_frames = 0
        self.current_frame = 0
        self.is_playing = False
        self.in_out = (None, None)

    def show_for_video(self) -> None:
        self.visible = True

    def show_for_video_with_io(self) -> None:
        self.visible = True
        self.io_visible = True

    def set_total_frames(self, total: int) -> None:
        self.total_frames = total

    def set_in_out_points(self, in_frame, out_frame) -> None:
        self.in_out = (in_frame, out_frame)

    def on_frame_changed(self, frame: int) -> None:
        self.current_frame = frame

    def on_play_state_changed(self, playing: bool) -> None:
        self.is_playing = playing


class _FakePlaybackController:
    def __init__(self) -> None:
        self.frame_changed = _FakeSignal()
        self.state_changed = _FakeSignal()
        self.timeline_duration = 100
        self.current_frame = 7
        self.is_playing = False
        self.playback_range = None

    def set_playback_range(self, start, end) -> None:
        self.playback_range = (start, end) if start is not None and end is not None else None
        self._playback_range_start = start
        self._playback_range_end = end

    def clear_playback_range(self) -> None:
        self.playback_range = None
        self._playback_range_start = None
        self._playback_range_end = None


class _CompositeControlController(PageManagementMixin):
    def __init__(self) -> None:
        self.play_toggles = 0
        self.scrub_moves = []
        self.playback_controller = _FakePlaybackController()
        self.timeline_panel = SimpleNamespace(
            get_playback_controller=lambda: self.playback_controller
        )
        self.composite_viewer = SimpleNamespace(video_control_bar=_FakeTransportBar())

    def on_video_seek(self, _frame: int) -> None:
        pass

    def on_video_scrub_started(self, frame: int) -> None:
        self.scrub_moves.append(("start", frame))

    def on_video_scrub_moved(self, frame: int) -> None:
        self.scrub_moves.append(("move", frame))

    def on_video_scrub_finished(self, frame: int) -> None:
        self.scrub_moves.append(("finish", frame))

    def on_video_play_toggled(self) -> None:
        self.play_toggles += 1

    def on_video_next_frame(self) -> None:
        pass

    def on_video_prev_frame(self) -> None:
        pass

    def on_video_first_frame(self) -> None:
        pass

    def on_video_last_frame(self) -> None:
        pass


class _CompositePlaybackFrameController(PageManagementMixin):
    def __init__(self) -> None:
        project, timeline, clip = _project_with_timeline_clip()
        self.current_page = "Composite"
        self._page_context_manager = PageContextManager()
        self._current_timeline_sequences = {timeline.timeline_id: timeline}
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self.composite_page = SimpleNamespace(
            _runtime_context=SimpleNamespace(timeline_frame=clip.timeline_start)
        )
        self.setup_calls = []
        self.timeline = timeline
        self.clip = clip
        context = self._page_context_manager.create_context_for_edit_to_composite(
            clip_id=clip.clip_id,
            composite_id="",
            asset_id=clip.asset_id,
            timeline_id=timeline.timeline_id,
            timeline_frame=clip.timeline_start,
            source_frame=clip.source_start,
            clip_local_frame=0,
            color_stack_id=clip.color_stack_id,
        )
        self._page_context_manager.set_current_context(context)

    def _setup_timeline_clip_context(self, context, composite_id, clip_id) -> None:
        self.setup_calls.append((context, composite_id, clip_id))

    def _clamp_composite_playback_to_clip_range(self, timeline_frame, clip) -> None:
        raise AssertionError("duplicate in-range frame should not clamp")


def test_timeline_composite_controls_follow_edit_playback_controller() -> None:
    controller = _CompositeControlController()
    bar = controller.composite_viewer.video_control_bar
    playback = controller.playback_controller

    controller._configure_composite_timeline_playback_controls(True)

    assert bar.visible is True
    assert bar.io_visible is True
    assert bar.total_frames == 100
    assert bar.current_frame == 7
    assert bar.is_playing is False

    playback.current_frame = 8
    playback.frame_changed.emit(8)
    playback.state_changed.emit("playing")
    bar.scrub_started.emit(9)
    bar.scrub_moved.emit(10)
    bar.scrub_finished.emit(10)
    bar.play_toggled.emit()

    assert bar.current_frame == 8
    assert bar.is_playing is True
    assert controller.play_toggles == 1
    assert controller.scrub_moves == [("start", 9), ("move", 10), ("finish", 10)]

    bar.mark_in_requested.emit()
    assert playback._playback_range_start == 8
    # 规格裁决（P1-APP-01）：单设入点时出点默认 clip 末帧（100 帧时间线
    # → 99），与 mark_play_out 的「in 默认 0」对称——播放引擎需要闭区间，
    # I 键按下后立即可播放到片尾。旧断言 (8, None) 属过期语义。
    assert bar.in_out == (8, 99)
    playback.current_frame = 24
    bar.mark_out_requested.emit()
    assert playback.playback_range == (8, 24)
    assert bar.in_out == (8, 24)
    bar.clear_in_out_requested.emit()
    assert playback.playback_range is None
    assert bar.in_out == (None, None)


def test_composite_playback_ignores_duplicate_timeline_frame() -> None:
    controller = _CompositePlaybackFrameController()

    controller._on_composite_playback_frame_changed(controller.clip.timeline_start)

    assert controller.setup_calls == []


class _SplitSegmentPlaybackController(PageManagementMixin):
    """Bound to the left blade segment; the right sibling shares the asset."""

    def __init__(self) -> None:
        project, timeline, clip = _project_with_timeline_clip()
        right = ClipInstance(
            clip_id="clip-right",
            asset_id=clip.asset_id,
            track_id=clip.track_id,
            timeline_start=clip.timeline_end,
            timeline_duration=40,
            source_start=clip.source_start + clip.source_duration,
            source_duration=40,
            composite_id="graph-right",
        )
        timeline = timeline.with_clip_added(clip.track_id, right)
        self.current_page = "Composite"
        self._page_context_manager = PageContextManager()
        self._current_timeline_sequences = {timeline.timeline_id: timeline}
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self.composite_page = SimpleNamespace(
            _runtime_context=SimpleNamespace(timeline_frame=clip.timeline_start)
        )
        self.setup_calls = []
        self.clamp_calls = []
        self.seeks = []
        self.timeline = timeline
        self.clip = clip
        self.right_clip = right
        self.playback = SimpleNamespace(is_playing=False)
        self.timeline_panel = SimpleNamespace(
            get_playback_controller=lambda: self.playback,
            _scrubbing_playhead=False,
        )
        context = self._page_context_manager.create_context_for_edit_to_composite(
            clip_id=clip.clip_id,
            composite_id="",
            asset_id=clip.asset_id,
            timeline_id=timeline.timeline_id,
            timeline_frame=clip.timeline_start,
            source_frame=clip.source_start,
            clip_local_frame=0,
            color_stack_id=clip.color_stack_id,
        )
        self._page_context_manager.set_current_context(context)

    def _setup_timeline_clip_context(self, context, composite_id, clip_id) -> None:
        self.setup_calls.append((context, composite_id, clip_id))

    def _clamp_composite_playback_to_clip_range(self, timeline_frame, clip) -> None:
        self.clamp_calls.append((timeline_frame, clip.clip_id))


def test_composite_scrub_follows_playhead_into_sibling_segment() -> None:
    """Blade-split scrub must rebind to the segment under the playhead.

    Regression: every out-of-range move used to pause+seek back to the bound
    segment boundary, pinning the playhead and freezing the viewer.
    """
    controller = _SplitSegmentPlaybackController()

    controller._on_composite_playback_frame_changed(controller.right_clip.timeline_start + 3)

    assert controller.clamp_calls == []
    assert len(controller.setup_calls) == 1
    context, composite_id, clip_id = controller.setup_calls[0]
    assert clip_id == controller.right_clip.clip_id
    # The sibling's own comp graph is used; the old binding's is not inherited.
    assert composite_id == "graph-right"
    assert context.clip_id == controller.right_clip.clip_id
    assert context.source_frame == controller.right_clip.source_start + 3


def test_composite_playback_tick_still_clamps_at_clip_boundary() -> None:
    """Transport playback (not scrubbing) keeps the boundary-stop behaviour."""
    controller = _SplitSegmentPlaybackController()
    controller.playback.is_playing = True
    controller.timeline_panel._scrubbing_playhead = False

    controller._on_composite_playback_frame_changed(controller.right_clip.timeline_start + 3)

    assert controller.clamp_calls == [
        (controller.right_clip.timeline_start + 3, controller.clip.clip_id)
    ]
    assert controller.setup_calls == []


def test_composite_scrub_in_gap_keeps_last_frame_without_bounce() -> None:
    """A playhead over a timeline gap must not seek back or rebind."""
    controller = _SplitSegmentPlaybackController()
    gap_frame = controller.clip.timeline_start - 10

    controller._on_composite_playback_frame_changed(gap_frame)

    assert controller.clamp_calls == []
    assert controller.setup_calls == []


def test_composite_clip_switch_same_asset_keeps_source_cache() -> None:
    """Same-asset segment switches must keep the hold texture (no GOP stall)."""
    cleared = []
    provider = SimpleNamespace(clear_source_cache=lambda: cleared.append(True))
    blanked = []
    viewer = SimpleNamespace(set_texture=lambda *a: blanked.append(a))
    node = SimpleNamespace(
        set_cached_texture=lambda tex: None,
        set_parameter=lambda name, value: None,
    )
    page = SimpleNamespace(
        _runtime_context=None,
        _timeline_clip_gpu_provider=None,
        _node_graph=SimpleNamespace(
            nodes={"in": SimpleNamespace(node_type="timeline_clip_input")},
            fps=24.0,
            duration_frames=0,
        ),
        _viewer=viewer,
        _is_active=False,
        _renderer=None,
        _last_texture_id=1,
        _comp_clip_timeline_start=0,
    )
    # Patch the node dict to our recording node.
    page._node_graph.nodes["in"] = node

    def _ctx(clip_id: str, asset_id: str):
        return SimpleNamespace(
            timeline_id="tl-context",
            clip_id=clip_id,
            asset_id=asset_id,
            fps=24.0,
            duration_frames=40,
            clip_local_frame=0,
            timeline_frame=0,
            clip_timeline_start=0,
        )

    left = _ctx("clip-left", "asset-1")
    right_same_asset = _ctx("clip-right", "asset-1")
    other_asset = _ctx("clip-other", "asset-2")

    CompositePage.set_timeline_clip_context(page, left, provider)
    assert cleared == [True]  # initial binding still clears
    assert blanked == [(0, 0, 0)]
    cleared.clear()
    blanked.clear()

    CompositePage.set_timeline_clip_context(page, right_same_asset, provider)
    assert cleared == []
    assert blanked == []

    CompositePage.set_timeline_clip_context(page, other_asset, provider)
    assert cleared == [True]
    assert blanked == [(0, 0, 0)]
