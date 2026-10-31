"""WD1-G6 批 D（AUD-2704..2708 Major×5 + GAP-002 测试补口）修复回归测试。

覆盖：

* AUD-2704 —— ``_on_grading_changed_for_color_stack`` 在 ``engine.layers``
  为空时不再 UnboundLocalError：``trackings``/``video_denoise`` 随其他
  快照字段一起预初始化为 None，空层提交全链路成功。
* AUD-2705 —— 标题模板 fallback（无 TimelineView 宿主）解包
  ``MacroCommand.apply`` 的 ``(timeline, macro)`` 二元组，
  ``edit_page._current_timeline`` 不再变成 tuple。
* AUD-2706 —— RenderCacheWorker 生命周期：注册时连接
  ``finished → deleteLater``；finish/fail 槽只在 ``isFinished()`` 为真时
  丢弃登记条目，未退出线程由 ``finished`` 驱动的 sweep 收尾。
* AUD-2707 —— ``_execute_timeline_edit_command`` 统一提交入口：有
  TimelineView 走 ``execute_command``（命令栈/undo），无视图回退直接
  apply（含 MacroCommand 元组解包）；属性面板 reorder 站点不再走
  EditPage 上恒假的 ``_command_stack`` 分支。
* AUD-2708 —— ``_CreateStampedColorStackCommand`` 把建栈+盖章（及本地
  副本的 v2 快照复制）并为单一撤销单元，undo/redo 间 stack_id 稳定；
  clip 绑定经时间线命令路径提交。三个入口
  （``_ensure_clip_has_color_stack`` / ``_on_use_shared_grade_requested`` /
  ``_on_make_local_grade_copy_requested``）全部原子化。
* GAP-002 —— edit_controller.py 高风险低覆盖补口（本文件新增 20+ 用例）。

环境：QT_QPA_PLATFORM=offscreen；全部用最小 mixin 宿主 + 替身，不碰 GPU。
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from unittest.mock import MagicMock

from src.app.controllers import edit_controller as ec
from src.app.controllers.edit_controller import (
    EditControllerMixin,
    _CreateStampedColorStackCommand,
)
from src.features.edit.core.commands import CommandStack
from src.features.edit.core.project import EditProject
from src.shared.project.color_stack import ColorStackData


# ═══════════════════════════════════════════════════════════════════════
# AUD-2704: empty engine.layers must not raise UnboundLocalError
# ═══════════════════════════════════════════════════════════════════════


class _GradingHost(EditControllerMixin):
    """最小宿主：只提供 _on_grading_changed_for_color_stack 触达的属性。"""

    def __init__(self, layers: List[Any]) -> None:
        self._current_edit_project = EditProject(project_id="p1", name="P")
        self.engine = SimpleNamespace(
            layers=layers,
            active_layer_index=0,
            current_raw_params={},
            tracking_store=None,
            video_denoise_params=None,
            render_pipeline=MagicMock(),
        )
        self.media_pool_panel = SimpleNamespace(
            get_command_stack=lambda: SimpleNamespace(
                execute=lambda command, project: project))
        self.timeline_preview_renderer = None
        self._schedule_idle_color_timeline_clip_strip_refresh = MagicMock()


class _RecordingUpdateCommand:
    """UpdateColorStackCommand 替身：记录 create 收到的 kwargs。"""

    recorded: List[Dict[str, Any]] = []

    @staticmethod
    def create(**kwargs: Any) -> SimpleNamespace:
        _RecordingUpdateCommand.recorded.append(kwargs)
        return SimpleNamespace(kwargs=kwargs)


@pytest.fixture
def recording_update_command(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingUpdateCommand.recorded = []
    monkeypatch.setattr(
        ec, "get_command_classes",
        lambda: {"UpdateColorStackCommand": _RecordingUpdateCommand},
    )


def test_grading_commit_with_empty_layers_does_not_raise(
        recording_update_command: None) -> None:
    """空层（源未加载）提交调色：旧代码在此 UnboundLocalError。"""
    host = _GradingHost(layers=[])

    host._on_grading_changed_for_color_stack(
        "stack-1", {"exposure": 0.5})

    assert len(_RecordingUpdateCommand.recorded) == 1
    kwargs = _RecordingUpdateCommand.recorded[0]
    # AUD-2704 核心：空层时两个快照字段回落 None 而不是崩溃。
    assert kwargs["trackings"] is None
    assert kwargs["video_denoise"] is None
    assert kwargs["layers"] is None
    assert kwargs["curves_data"] is None
    # 提交本身完成：命令栈执行 + dirty 标记 + 引擎渲染管线触发。
    assert host.edit_project_dirty is True
    host.engine.render_pipeline.assert_called_once()


def test_grading_commit_with_layers_collects_tracking_and_denoise(
        recording_update_command: None) -> None:
    """有活动层时 trackings/video_denoise 照常随栈提交持久化。"""
    layer = SimpleNamespace(
        curves_data={"curves": 1},
        hsl_curve_data=None,
        mixer_params=None,
        soft_clip_params=None,
        active_lut_path="/luts/a.cube",
        lut_intensity=0.5,
    )
    layer.to_dict = lambda parent=None: {"snapshot": True}  # type: ignore[method-assign]
    host = _GradingHost(layers=[layer])
    host.engine.tracking_store = SimpleNamespace(
        to_dict=lambda: {"frames": {"f1": 3}})
    host.engine.video_denoise_params = SimpleNamespace(
        to_dict=lambda: {"enabled": True})

    host._on_grading_changed_for_color_stack(
        "stack-1", {"exposure": 0.5})

    kwargs = _RecordingUpdateCommand.recorded[0]
    assert kwargs["trackings"] == {"frames": {"f1": 3}}
    assert kwargs["video_denoise"] == {"enabled": True}
    assert kwargs["layers"] == [{"snapshot": True}]
    assert kwargs["active_layer_index"] == 0
    # 引擎层参数同步为最新提交值。
    assert layer.grading_params == {"exposure": 0.5}


# ═══════════════════════════════════════════════════════════════════════
# AUD-2705: title-template fallback must unpack MacroCommand's tuple
# ═══════════════════════════════════════════════════════════════════════

_RESULT_TIMELINE = SimpleNamespace(
    timeline_id="t9",
    subtitle_tracks=[],
    get_clip_by_id=lambda cid: None,
)


class _TitleMacroCommand:
    """MacroCommand 替身：apply 返回 (timeline, command) 二元组（真契约）。"""

    def __init__(self, label: str, commands: List[Any]) -> None:
        self.label = label
        self.sub_commands = commands

    def apply(self, timeline: Any) -> Any:
        return (_RESULT_TIMELINE, self)

    @staticmethod
    def create(label: str, commands: List[Any]) -> "_TitleMacroCommand":
        return _TitleMacroCommand(label, commands)


class _TitleHost(EditControllerMixin):
    """无 TimelineView 宿主：走 fallback 直接 apply 分支。"""

    def __init__(self, timeline: Any) -> None:
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self.timeline_panel = None
        self.status_bar = MagicMock()


@pytest.fixture
def title_template_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.features.edit.core.commands as commands_mod

    preset = SimpleNamespace(
        preset_id="p1", label="Big Title", style=None)
    add_cmd = SimpleNamespace(_created_clip_id=None)

    monkeypatch.setattr(
        commands_mod, "AddTrackCommand",
        SimpleNamespace(create=lambda *a, **k: SimpleNamespace()))
    monkeypatch.setattr(
        commands_mod, "AddSubtitleClipCommand",
        SimpleNamespace(create=lambda **k: add_cmd))
    monkeypatch.setattr(
        commands_mod, "MacroCommand", _TitleMacroCommand)
    monkeypatch.setattr(
        commands_mod, "UpdateSubtitleClipCommand",
        SimpleNamespace(create=lambda **k: SimpleNamespace()))
    monkeypatch.setattr(
        "src.features.edit.core.captions.style_preset.BUILTIN_PRESETS",
        [preset])
    monkeypatch.setattr(
        "src.features.edit.core.title_templates.list_user_title_templates",
        lambda: [])


def test_title_template_fallback_stores_timeline_not_tuple(
        title_template_fakes: None) -> None:
    """无 TimelineView 宿主：fallback 解包 macro 二元组（AUD-2705）。"""
    timeline = SimpleNamespace(timeline_id="t1", subtitle_tracks=[])
    host = _TitleHost(timeline)

    host._on_title_template_selected("p1")

    # 旧代码把 (timeline, macro) 元组整个存进 _current_timeline，
    # 下一次 get_clip_by_id 即 AttributeError: 'tuple' object ...
    assert host.edit_page._current_timeline is _RESULT_TIMELINE
    assert not isinstance(host.edit_page._current_timeline, tuple)
    host.status_bar.showMessage.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# AUD-2706: RenderCacheWorker lifetime
# ═══════════════════════════════════════════════════════════════════════


class _WorkerSignals:
    def __init__(self) -> None:
        self.connected: List[Any] = []

    def connect(self, slot: Any) -> None:
        self.connected.append(slot)


class _FakeCacheWorker:
    def __init__(self) -> None:
        self.progress_changed = _WorkerSignals()
        self.finished_ok = _WorkerSignals()
        self.failed = _WorkerSignals()
        self.finished = _WorkerSignals()
        self.deleteLater = MagicMock()
        self.start = MagicMock()
        self._is_finished = False

    def isFinished(self) -> bool:
        return self._is_finished


class _CacheHost(EditControllerMixin):
    def __init__(self, worker: _FakeCacheWorker) -> None:
        timeline = SimpleNamespace(timeline_id="t1")
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self._current_edit_project = SimpleNamespace(
            render_cache_manifests={})
        self.status_bar = MagicMock()
        self._worker = worker


@pytest.fixture
def cache_worker(monkeypatch: pytest.MonkeyPatch) -> _FakeCacheWorker:
    from src.features.edit import facade as edit_facade

    worker = _FakeCacheWorker()
    monkeypatch.setattr(
        edit_facade, "create_render_cache_worker",
        lambda manifest, edit_project, timeline_sequences: worker)
    return worker


def test_render_cache_worker_gets_deletelater_on_start(
        cache_worker: _FakeCacheWorker) -> None:
    """启动时连接 finished → deleteLater（QThread 自管理析构模式）。"""
    host = _CacheHost(cache_worker)
    manifest = SimpleNamespace(cache_id="cache-1")

    assert host._start_render_cache_worker(manifest) is True

    # AUD-2706: 无 parent 的 QThread 必须挂自管理析构钩子。
    assert cache_worker.deleteLater in cache_worker.finished.connected
    # 登记 + 启动照旧。
    assert host._render_cache_workers["cache-1"] is cache_worker
    cache_worker.start.assert_called_once()


def test_release_keeps_entry_until_thread_really_finished(
        cache_worker: _FakeCacheWorker) -> None:
    """isFinished() 为假时不丢登记条目（防运行中析构竞态）。"""
    host = _CacheHost(cache_worker)
    manifest = SimpleNamespace(cache_id="cache-1")
    host._start_render_cache_worker(manifest)

    # finish/fail 槽可能在 QThread 完全退出前被队列投递执行。
    cache_worker._is_finished = False
    host._release_render_cache_worker_slot("cache-1")
    assert "cache-1" in host._render_cache_workers

    # 线程真正退出后才允许丢弃最后引用。
    cache_worker._is_finished = True
    host._release_render_cache_worker_slot("cache-1")
    assert "cache-1" not in host._render_cache_workers


def test_finished_sweep_removes_entry_by_identity(
        cache_worker: _FakeCacheWorker) -> None:
    """finished 信号驱动的 sweep 按 sender 身份移除登记条目。"""
    host = _CacheHost(cache_worker)
    host._start_render_cache_worker(SimpleNamespace(cache_id="cache-1"))
    # 非 QObject 测试宿主没有 Qt sender()——直接注入。
    host.sender = lambda: cache_worker  # type: ignore[method-assign]

    host._on_render_cache_worker_thread_finished()

    assert "cache-1" not in host._render_cache_workers


def test_render_cache_failed_keeps_running_worker_registered(
        cache_worker: _FakeCacheWorker) -> None:
    """failed 槽经真实处理函数走 guarded release（线程未退出 → 保留）。"""
    host = _CacheHost(cache_worker)
    host._start_render_cache_worker(SimpleNamespace(cache_id="cache-1"))
    cache_worker._is_finished = False

    host._on_render_cache_failed("cache-1", "boom")

    assert "cache-1" in host._render_cache_workers


def test_render_cache_finished_releases_once_thread_exited(
        cache_worker: _FakeCacheWorker) -> None:
    """finished 槽在线程已退出时移除登记条目。"""
    host = _CacheHost(cache_worker)
    host._start_render_cache_worker(SimpleNamespace(cache_id="cache-1"))
    cache_worker._is_finished = True

    host._on_render_cache_finished("cache-1", "/tmp/out.mov")

    assert "cache-1" not in host._render_cache_workers


# ═══════════════════════════════════════════════════════════════════════
# AUD-2707: unified timeline-command execution entry
# ═══════════════════════════════════════════════════════════════════════


class _FakeEditView:
    def __init__(self, timeline: Any) -> None:
        self._timeline = timeline
        self.executed: List[Any] = []

    def execute_command(self, command: Any) -> None:
        self.executed.append(command)
        self._timeline = command.apply(self._timeline)


class _LeafCommand:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.applied_to: List[Any] = []

    def apply(self, timeline: Any) -> Any:
        self.applied_to.append(timeline)
        return self.result


class _TupleCommand(_LeafCommand):
    """复合命令替身：apply 返回 (timeline, updated_command)。"""

    def apply(self, timeline: Any) -> Any:
        self.applied_to.append(timeline)
        return (self.result, self)


class _EditHost(EditControllerMixin):
    def __init__(self, timeline: Any, view: Any = None) -> None:
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        if view is not None:
            self.timeline_panel = SimpleNamespace(
                get_timeline_view=lambda: view)
        else:
            self.timeline_panel = None
        self._sync_timeline_to_views = MagicMock()  # type: ignore[method-assign]


def test_execute_via_timeline_view_command_stack() -> None:
    """有 TimelineView：命令经 execute_command 提交（undo 栈单一入口）。"""
    old_timeline = SimpleNamespace(name="old")
    new_timeline = SimpleNamespace(name="new")
    view = _FakeEditView(old_timeline)
    host = _EditHost(old_timeline, view)
    command = _LeafCommand(new_timeline)

    result = host._execute_timeline_edit_command(command, old_timeline)

    assert view.executed == [command]
    assert result is new_timeline
    assert view._timeline is new_timeline
    # 视图路径不需要手动同步（信号链负责）。
    host._sync_timeline_to_views.assert_not_called()


def test_execute_fallback_applies_and_syncs() -> None:
    """无 TimelineView：直接 apply + 发布到 edit_page + 同步视图。"""
    old_timeline = SimpleNamespace(name="old")
    new_timeline = SimpleNamespace(name="new")
    host = _EditHost(old_timeline, view=None)
    command = _LeafCommand(new_timeline)

    result = host._execute_timeline_edit_command(command, old_timeline)

    assert command.applied_to == [old_timeline]
    assert result is new_timeline
    assert host.edit_page._current_timeline is new_timeline
    host._sync_timeline_to_views.assert_called_once_with(new_timeline)


def test_execute_fallback_unpacks_compound_tuple() -> None:
    """fallback 对 MacroCommand 的 (timeline, command) 元组解包。"""
    old_timeline = SimpleNamespace(name="old")
    new_timeline = SimpleNamespace(name="new")
    host = _EditHost(old_timeline, view=None)
    command = _TupleCommand(new_timeline)

    result = host._execute_timeline_edit_command(command, old_timeline)

    assert result is new_timeline
    assert not isinstance(result, tuple)
    assert host.edit_page._current_timeline is new_timeline


def test_execute_view_without_timeline_falls_back() -> None:
    """视图存在但无时间线：不丢命令，回退直接 apply。"""
    old_timeline = SimpleNamespace(name="old")
    new_timeline = SimpleNamespace(name="new")
    view = SimpleNamespace(execute_command=MagicMock(), _timeline=None)
    host = _EditHost(old_timeline, view)
    command = _LeafCommand(new_timeline)

    result = host._execute_timeline_edit_command(command, old_timeline)

    # execute_command 拒绝空时间线时命令必须经 fallback 落地。
    assert command.applied_to == [old_timeline]
    assert result is new_timeline
    assert host.edit_page._current_timeline is new_timeline


class _FakeReorderCommand(_LeafCommand):
    @staticmethod
    def create(track_id: str, clip_id: str, effect_id: str,
               new_order: int, timeline: Any) -> "_FakeReorderCommand":
        return _FakeReorderCommand(SimpleNamespace(name="reordered"))


class _ReorderHost(_EditHost):
    def __init__(self, timeline: Any, view: Any) -> None:
        super().__init__(timeline, view)
        self._update_properties_panel_after_effect_change = MagicMock()  # type: ignore[method-assign]
        self._request_program_viewer_update = MagicMock()  # type: ignore[method-assign]


def test_effect_reorder_site_commits_via_view(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """AUD-2707 站点级：属性面板 reorder 不再走恒假的 _command_stack 分支。"""
    monkeypatch.setattr(
        ec, "get_command_classes",
        lambda: {"ReorderEffectCommand": _FakeReorderCommand})
    old_timeline = SimpleNamespace(
        name="old",
        get_clip_by_id=lambda cid: SimpleNamespace(track_id="v1"))
    view = _FakeEditView(old_timeline)
    host = _ReorderHost(old_timeline, view)

    host._on_edit_effect_reorder_requested("c1", "e1", 1)

    # 命令经 TimelineView.execute_command 提交（undo 栈/自动保存入口）。
    assert len(view.executed) == 1
    assert view._timeline is not old_timeline
    host._update_properties_panel_after_effect_change.assert_called_once_with("c1")


# ═══════════════════════════════════════════════════════════════════════
# AUD-2708: atomic stamped color-stack creation
# ═══════════════════════════════════════════════════════════════════════


class _FakeColorClip:
    def __init__(self, clip_id: str, asset_id: str,
                 color_stack_id: str = "") -> None:
        self.clip_id = clip_id
        self.asset_id = asset_id
        self.track_id = "v1"
        self.color_stack_id = color_stack_id
        self.color_shared = False

    def with_stack(self, stack_id: str, shared: bool) -> "_FakeColorClip":
        clone = _FakeColorClip(self.clip_id, self.asset_id, stack_id)
        clone.color_shared = shared
        return clone


class _FakeColorTrack:
    def __init__(self, track_id: str, clips: List[_FakeColorClip]) -> None:
        self.track_id = track_id
        self.clips = list(clips)

    def get_clip_by_id(self, clip_id: str) -> Optional[_FakeColorClip]:
        return next((c for c in self.clips if c.clip_id == clip_id), None)


class _FakeColorTimeline:
    def __init__(self, timeline_id: str,
                 tracks: List[_FakeColorTrack]) -> None:
        self.timeline_id = timeline_id
        self.tracks = list(tracks)

    @property
    def all_tracks(self) -> List[_FakeColorTrack]:
        return list(self.tracks)

    def get_clip_by_id(self, clip_id: str) -> Optional[_FakeColorClip]:
        for track in self.tracks:
            clip = track.get_clip_by_id(clip_id)
            if clip is not None:
                return clip
        return None

    def with_clip_stack(self, clip_id: str, stack_id: str,
                        shared: bool) -> "_FakeColorTimeline":
        new_tracks = [
            _FakeColorTrack(t.track_id, [
                c.with_stack(stack_id, shared) if c.clip_id == clip_id else c
                for c in t.clips
            ])
            for t in self.tracks
        ]
        return _FakeColorTimeline(self.timeline_id, new_tracks)


class _FakeBindCommand:
    """UpdateClipColorStackCommand 替身（时间线域）。"""

    def __init__(self, track_id: str, clip_id: str, stack_id: str,
                 shared: bool) -> None:
        self.track_id = track_id
        self.clip_id = clip_id
        self.new_color_stack_id = stack_id
        self.new_color_shared = shared

    def apply(self, timeline: Any) -> Any:
        return timeline.with_clip_stack(
            self.clip_id, self.new_color_stack_id, self.new_color_shared)

    def undo(self, timeline: Any) -> Any:
        return timeline

    @staticmethod
    def create(track_id: str, clip_id: str, new_color_stack_id: str,
               new_color_shared: bool) -> "_FakeBindCommand":
        return _FakeBindCommand(
            track_id, clip_id, new_color_stack_id, new_color_shared)


class _ColorHost(EditControllerMixin):
    """headless 调色宿主：真实 EditProject + 真实项目命令栈 + 替身时间线。"""

    def __init__(self, project: EditProject,
                 timeline: _FakeColorTimeline) -> None:
        self._current_edit_project = project
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self.project_stack = CommandStack()
        self.media_pool_panel = SimpleNamespace(
            get_command_stack=lambda: self.project_stack)
        self.timeline_preview_renderer = None
        self.timeline_panel = None
        self._sync_timeline_to_views = MagicMock()  # type: ignore[method-assign]


@pytest.fixture
def bind_command_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ec, "get_command_classes",
        lambda: {"UpdateClipColorStackCommand": _FakeBindCommand})


def _clip_timeline() -> _FakeColorTimeline:
    return _FakeColorTimeline(
        "t1", [_FakeColorTrack("v1", [_FakeColorClip("c1", "a1")])])


class TestStampedCreateCommand:
    """_CreateStampedColorStackCommand 单元：单事务 + redo 稳定 id。"""

    def test_apply_undo_redo_single_unit_with_stable_id(self) -> None:
        project = EditProject(project_id="p1", name="P")
        stack = CommandStack()
        command = _CreateStampedColorStackCommand.create(
            asset_id="a1", name="S",
            stamp_fields={"scope": "asset_default",
                          "source_kind": "asset_default"})

        applied = stack.execute(command, project)
        stack_id = command.created_stack_id
        assert stack_id in applied.color_stacks
        created = applied.color_stacks[stack_id]
        assert created.scope == "asset_default"
        assert created.source_kind == "asset_default"
        assert created.asset_id == "a1"

        # 单一撤销单元：undo 整栈移除。
        undone = stack.undo(applied)
        assert stack_id not in undone.color_stacks

        # redo：同一 stack_id + 盖章完整恢复（CreateColorStackCommand
        # 每次重放生成新 id，redo 后绑定必然悬空——本命令修复该缺陷）。
        redone = stack.redo(undone)
        assert stack_id in redone.color_stacks
        restored = redone.color_stacks[stack_id]
        assert restored.scope == "asset_default"
        assert restored is created

    def test_undo_restores_displaced_stack(self) -> None:
        """同 id 栈被他人替换时：apply 记录 displaced，undo 恢复原栈。"""
        project = EditProject(project_id="p1", name="P")
        command = _CreateStampedColorStackCommand.create(
            asset_id=None, name="S")
        applied = command.apply(project)
        stack_id = command.created_stack_id

        other = ColorStackData(stack_id=stack_id, name="Other")
        replaced = type(applied)(
            **{**applied.__dict__, "color_stacks": {stack_id: other}})
        reapplied = command.apply(replaced)
        assert reapplied.color_stacks[stack_id] is command._stack

        undone = command.undo(reapplied)
        assert undone.color_stacks[stack_id] is other

    def test_undo_noop_when_stack_replaced_by_other(self) -> None:
        """undo 时同 id 已非本命令产物：不破坏他人状态。"""
        project = EditProject(project_id="p1", name="P")
        command = _CreateStampedColorStackCommand.create(
            asset_id=None, name="S")
        applied = command.apply(project)
        stack_id = command.created_stack_id

        other = ColorStackData(stack_id=stack_id, name="Later")
        replaced = type(applied)(
            **{**applied.__dict__, "color_stacks": {stack_id: other}})

        result = command.undo(replaced)
        # 他人的栈保持原样。
        assert result.color_stacks[stack_id] is other


class TestEnsureClipColorStackAtomic:
    """_ensure_clip_has_color_stack：建栈+盖章+绑定原子化。"""

    def test_create_stamps_and_binds_atomically(
            self, bind_command_fake: None) -> None:
        project = EditProject(project_id="p1", name="P")
        timeline = _clip_timeline()
        host = _ColorHost(project, timeline)

        stack_id = host._ensure_clip_has_color_stack("c1", "a1")

        assert stack_id
        stack = host._current_edit_project.color_stacks[stack_id]
        assert stack.scope == "clip_local"
        assert stack.source_kind == "timeline_clip"
        assert stack.clip_id == "c1"
        assert stack.timeline_id == "t1"
        # 绑定发布到当前时间线。
        bound = host.edit_page._current_timeline.get_clip_by_id("c1")
        assert bound is not None and bound.color_stack_id == stack_id

    def test_undo_removes_stack_single_unit(
            self, bind_command_fake: None) -> None:
        """媒体池单步 undo 即整栈移除（旧实现盖章不进栈，redo 丢盖章）。"""
        project = EditProject(project_id="p1", name="P")
        timeline = _clip_timeline()
        host = _ColorHost(project, timeline)
        stack_id = host._ensure_clip_has_color_stack("c1", "a1")

        undone = host.project_stack.undo(host._current_edit_project)

        assert stack_id not in undone.color_stacks

        redone = host.project_stack.redo(undone)
        assert stack_id in redone.color_stacks
        assert redone.color_stacks[stack_id].scope == "clip_local"
        assert redone.color_stacks[stack_id].clip_id == "c1"


class TestSharedGradeHandler:
    """_on_use_shared_grade_requested：按需建 asset_default 栈为单事务。"""

    def test_creates_stamped_asset_default_stack(
            self, bind_command_fake: None) -> None:
        project = EditProject(project_id="p1", name="P")
        timeline = _clip_timeline()
        host = _ColorHost(project, timeline)

        host._on_use_shared_grade_requested("c1", "v1")

        new_stacks = [
            s for s in host._current_edit_project.color_stacks.values()
            if s.scope == "asset_default"
        ]
        assert len(new_stacks) == 1
        created = new_stacks[0]
        assert created.asset_id == "a1"
        assert created.source_kind == "asset_default"
        # clip 绑定到素材默认栈且共享。
        bound = host.edit_page._current_timeline.get_clip_by_id("c1")
        assert bound is not None
        assert bound.color_stack_id == created.stack_id
        assert bound.color_shared is True

        # 单步 undo：栈移除；redo：同 id + 盖章恢复。
        undone = host.project_stack.undo(host._current_edit_project)
        assert created.stack_id not in undone.color_stacks
        redone = host.project_stack.redo(undone)
        assert redone.color_stacks[created.stack_id].scope == "asset_default"


class TestLocalGradeCopyHandler:
    """_on_make_local_grade_copy_requested：本地副本建栈走命令栈。"""

    def _project_with_source(self) -> EditProject:
        source = ColorStackData(
            stack_id="src-1", asset_id="a1", name="Source",
            grading_params={"exposure": 0.75},
            layers=({"layer": 1},),
            active_layer_index=0,
            trackings={"frames": {"f1": 2}},
            raw_params={"white": 2.0},
        )
        return EditProject(
            project_id="p1", name="P", color_stacks={"src-1": source})

    def test_local_copy_is_undoable_and_carries_snapshot(
            self, bind_command_fake: None) -> None:
        project = self._project_with_source()
        timeline = _clip_timeline()
        timeline.tracks[0].clips[0].color_stack_id = "src-1"
        host = _ColorHost(project, timeline)

        host._on_make_local_grade_copy_requested("c1", "v1")

        stacks = host._current_edit_project.color_stacks
        assert "src-1" in stacks  # 源栈原样保留
        local_ids = [
            sid for sid, s in stacks.items()
            if s.scope == "clip_local" and s.clip_id == "c1"
        ]
        assert len(local_ids) == 1
        local = stacks[local_ids[0]]
        # P1-COLOR-16 v2 完整快照随本地副本复制。
        assert local.grading_params == {"exposure": 0.75}
        assert local.layers == ({"layer": 1},)
        assert local.trackings == {"frames": {"f1": 2}}
        assert local.raw_params == {"white": 2.0}
        assert local.timeline_id == "t1"
        # 绑定切换到本地栈（非共享）。
        bound = host.edit_page._current_timeline.get_clip_by_id("c1")
        assert bound is not None
        assert bound.color_stack_id == local.stack_id
        assert bound.color_shared is False

        # AUD-2708 核心：本地建栈经命令栈——单步 undo 即移除本地栈。
        undone = host.project_stack.undo(host._current_edit_project)
        assert local.stack_id not in undone.color_stacks
        assert "src-1" in undone.color_stacks
        # redo：同 id 恢复，快照仍在。
        redone = host.project_stack.redo(undone)
        restored = redone.color_stacks[local.stack_id]
        assert restored.grading_params == {"exposure": 0.75}
        assert restored.trackings == {"frames": {"f1": 2}}
