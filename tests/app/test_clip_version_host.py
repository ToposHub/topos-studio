"""WIRE-01/02 · 片段版本宿主接线验收（真实命令栈 + 真实 UI 协议驱动）。

全部操作经 ``strip.version_ui.submit`` → ``operation_requested`` → 宿主
→ ``finish`` 正典链（与生产菜单同路径），覆盖计划 §6 完成定义与 §7.3
矩阵中可无头验证的行：

- 读取回填权威集合（connected/loaded/versions/active/in_use/capabilities）；
- CREATE：空域建栈（内容=当前 live，含 flush 后参数）+ 默认版本同事务；
- LOAD：同域激活内容互换；跨域 = 切引用 + 激活；未保存修改须确认；
- RENAME/DELETE：仅表操作零重解码；删除保护（default/active/最后）；
- DELETE_ALL：保留 default 并加载（hydrate + 缩略图失效）；
- COPY_TO_SCOPE：空目标域初始化 + 复制同事务，目标集合回填；
- 失败/过期零提交（revision 守卫、重名、版本不存在、工程切换）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.app.controllers.clip_version_host import ClipVersionRequestHost
from src.features.color.ui.clip_version_ui import VersionOperation, VersionScope
from src.features.color.ui.timeline_clip_strip import TimelineClipStrip
from src.features.edit.core.color_version_domains import (
    VersionDomain, resolve_clip_binding, resolve_remote_stack_id,
)
from src.features.edit.core.color_versions import (
    DEFAULT_VERSION_NAME, SaveColorVersionCommand, versions_state,
)
from src.features.edit.core.commands import (
    ProjectCommandStack, UpdateClipColorStackCommand,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, TimelineSequence,
)
from src.shared.project.color_stack import ColorStackData

pytestmark = pytest.mark.app


def _local_stack(stack_id, clip_id="c1", asset_id="a1",
                 exposure=5.0) -> ColorStackData:
    return ColorStackData(
        stack_id=stack_id, asset_id=asset_id, name=stack_id,
        scope="clip_local", source_kind="timeline_clip",
        timeline_id="tl1", clip_id=clip_id,
        grading_params={"exposure": exposure})


def _remote_stack(stack_id, asset_id="a1",
                  exposure=9.0) -> ColorStackData:
    return ColorStackData(
        stack_id=stack_id, asset_id=asset_id, name=stack_id,
        scope="asset_default", source_kind="asset_default",
        grading_params={"exposure": exposure})


class _MediaPoolPanel(QObject):
    """媒体池替身：project_changed 用真实 pyqtSignal（审查 C1 防回归
    ——直接调用 bound signal 会 TypeError，普通方法替身会掩盖）。"""

    project_changed = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._stack = ProjectCommandStack()
        self.set_project_calls = []
        self.changed_projects = []
        self.project_changed.connect(self._on_changed)

    def _on_changed(self, project):
        self.changed_projects.append(project)

    def get_command_stack(self):
        return self._stack

    def set_project(self, project):
        self.set_project_calls.append(project)


class _GradingPanel:
    def __init__(self):
        self.current_stack_id = None
        self.save_calls = []
        self.on_save = None

    def trigger_save(self):
        self.save_calls.append(self.current_stack_id)
        if self.on_save is not None:
            self.on_save()


class _HostShell:
    """duck-typed MainWindow 替身：真实工程/命令栈/strip + 记录器。"""

    def __init__(self, project, timeline, strip, *, session_clip="c1"):
        self._current_edit_project = project
        self.edit_project_dirty = False
        self.media_pool_panel = _MediaPoolPanel()
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self.strip = strip
        self.color_timeline_clip_strip = strip
        self.grading_panel = _GradingPanel()
        self._grading_save_stack_id = None
        self._color_stack_hydration_in_progress = False
        self.renderer_calls: list = []
        self.timeline_preview_renderer = SimpleNamespace(
            update_color_stacks=lambda p: self.renderer_calls.append(p))
        self.hydrate_calls: list = []
        self.refresh_calls: list = []
        self.preview_sync_calls: list = []
        session = SimpleNamespace(
            clip_id=session_clip,
            color_stack_id=(timeline.video_tracks[0].clips[0].color_stack_id))
        self._color_session_manager = SimpleNamespace(
            get_current_session=lambda: session)

    # ---- 宿主方法（镜像真实 mixin 语义的最小实现） ----

    def _load_color_stack_to_grading_panel(self, stack_id):
        self.hydrate_calls.append(stack_id)

    def _schedule_color_timeline_clip_preview_sync(self, clip_id):
        self.preview_sync_calls.append(clip_id)

    def refresh_color_timeline_clip_strip(self):
        """真实路径：用最新工程重建片段栏条目（绑定/缓存失效）。"""
        self.refresh_calls.append(True)
        project = self._current_edit_project
        sequence = project.timelines.get("tl1")
        self.edit_page._current_timeline = sequence
        self.strip.set_project_and_timeline(project, sequence)

    def _apply_clip_color_stack_change(self, track_id, clip_id, stack_id,
                                       *, shared):
        timeline = self.edit_page._current_timeline
        command = UpdateClipColorStackCommand.create(
            track_id=track_id, clip_id=clip_id,
            new_color_stack_id=stack_id, new_color_shared=shared)
        result = command.apply(timeline)
        new_timeline = result if not isinstance(result, tuple) else result[0]
        self.edit_page._current_timeline = new_timeline
        new_timelines = dict(self._current_edit_project.timelines)
        new_timelines["tl1"] = new_timeline
        self._current_edit_project = replace(
            self._current_edit_project, timelines=new_timelines)
        return True


def _build_project():
    """c1（本地栈 loc-1，素材 a1）+ c2（共享 rem-1，素材 a1）+
    c3（本地栈 loc-2，素材 a2——无共享栈，REMOTE 为真空域）。"""
    clips = (
        ClipInstance("c1", "a1", "v1", 0, 10, 0, 10,
                     color_stack_id="loc-1"),
        ClipInstance("c2", "a1", "v1", 10, 10, 0, 10,
                     color_stack_id="rem-1", color_shared=True),
        ClipInstance("c3", "a2", "v1", 20, 10, 0, 10,
                     color_stack_id="loc-2"),
    )
    timeline = TimelineSequence(
        timeline_id="tl1", name="TL", fps=24.0,
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=clips),))
    project = EditProject(
        project_id="p1", name="P", revision=3,
        assets={"a1": MediaAsset("a1", "/m/a1.mov", "video", "a1", {}),
                "a2": MediaAsset("a2", "/m/a2.mov", "video", "a2", {})},
        color_stacks={"loc-1": _local_stack("loc-1"),
                      "rem-1": _remote_stack("rem-1"),
                      "loc-2": _local_stack("loc-2", clip_id="c3",
                                            asset_id="a2", exposure=3.0)},
        timelines={"tl1": timeline})
    return project, timeline


@pytest.fixture
def env(qtbot):
    project, timeline = _build_project()
    strip = TimelineClipStrip()
    qtbot.addWidget(strip)
    strip.set_project_and_timeline(project, timeline)
    shell = _HostShell(project, timeline, strip)
    host = ClipVersionRequestHost(shell)
    host.connect_strip(strip)
    return shell, host, strip


def _target(shell, clip_id="c1"):
    strip = shell.strip
    entry = next(e for e in strip._all_entries if e.clip_id == clip_id)
    return strip._version_target(entry)


def _drive(shell, scope, operation, *, clip_id="c1", version_id=None,
           name="", destination=None, discard=False):
    """经正典 UI 协议驱动一次操作：读取回填 → submit → 宿主 → finish。

    返回 (error_text, request_id)。submit 前置校验失败返回
    (last_error, None)——同样是"零提交"证据。跨域复制会先读目标域
    （与真实菜单 open_action 行为一致）。
    """
    strip = shell.strip
    target = _target(shell, clip_id)
    outcomes = []

    def _record(result):
        outcomes.append(result.error)

    strip.version_ui.operation_finished.connect(_record)
    strip.version_ui.state_requested.emit(target, scope)  # 菜单展开即读取
    if destination is not None:
        strip.version_ui.state_requested.emit(target, destination)
    request_id = strip.version_ui.submit(
        target, scope, operation, version_id=version_id, name=name,
        destination_scope=destination, discard_unsaved=discard)
    if request_id is None:
        error = strip.version_ui.last_error
        strip.version_ui.operation_finished.disconnect(_record)
        return error, None
    error = outcomes[0] if outcomes else ""
    strip.version_ui.operation_finished.disconnect(_record)
    return error, request_id


def _save(shell, stack_id, name, *, exposure=None):
    project = shell._current_edit_project
    if exposure is not None:
        stack = replace(project.color_stacks[stack_id],
                        grading_params={"exposure": exposure})
        project = project.with_color_stacks({**project.color_stacks,
                                             stack_id: stack})
    project = SaveColorVersionCommand.create(project, stack_id, name).apply(
        project)
    shell._current_edit_project = project
    return project


class TestReadVersions:
    def test_read_local_collection_backfilled(self, env):
        shell, host, strip = env
        target = _target(shell)
        host.read_versions(target, VersionScope.LOCAL)
        collection = strip.version_ui.collection(target, VersionScope.LOCAL)
        assert collection.connected and collection.loaded
        assert collection.in_use  # c1 用本地栈
        assert collection.revision == 3
        assert VersionOperation.CREATE in collection.capabilities
        assert collection.versions == ()  # 空表（未初始化）
        assert VersionOperation.DELETE_ALL not in collection.capabilities
        assert collection.target is not None

    def test_read_remote_collection_marks_not_in_use(self, env):
        shell, host, strip = env
        target = _target(shell)
        host.read_versions(target, VersionScope.REMOTE)
        collection = strip.version_ui.collection(target, VersionScope.REMOTE)
        assert collection.connected and not collection.in_use

    def test_stale_project_read_ignored(self, env):
        shell, host, strip = env
        stale = replace(_target(shell), project_id="other")
        host.read_versions(stale, VersionScope.LOCAL)
        collection = strip.version_ui.collection(stale, VersionScope.LOCAL)
        assert not collection.connected  # 未被回填


class TestCreate:
    def test_create_saves_flushed_live_params(self, env):
        shell, host, strip = env
        # 面板正在编辑 loc-1 且有未落库手势（真实 flush 语义：trigger_save）
        shell.grading_panel.current_stack_id = "loc-1"
        shell._grading_save_stack_id = "loc-1"

        def commit_pending():
            project = shell._current_edit_project
            stack = replace(project.color_stacks["loc-1"],
                            grading_params={"exposure": 7.5})
            shell._current_edit_project = project.with_color_stacks(
                {**project.color_stacks, "loc-1": stack})
        shell.grading_panel.on_save = commit_pending

        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.CREATE, name="Warm")
        assert error == ""
        project = shell._current_edit_project
        state = versions_state(project, "loc-1")
        assert state["versions"]["Warm"]["grading_params"][
            "exposure"] == 7.5  # flush 后的最后参数，非旧值 5.0
        assert state["active"] == "Warm"
        assert shell.edit_project_dirty
        assert len(shell.media_pool_panel.set_project_calls) >= 1
        # 审查 C1 防回归：真实 pyqtSignal 广播必须成功（.emit 路径）
        assert shell.media_pool_panel.changed_projects
        assert shell.media_pool_panel.changed_projects[-1] \
            is shell._current_edit_project

    def test_create_in_empty_remote_domain_initializes_default(self, env):
        """c3（素材 a2）REMOTE 为真空域：建共享栈 + 默认版本 + 用户版本，
        内容 = 镜头当前 live（loc-2 的 3.0），不切换使用域。"""
        shell, host, strip = env
        error, _rid = _drive(shell, VersionScope.REMOTE,
                             VersionOperation.CREATE, name="SharedLook",
                             clip_id="c3")
        assert error == ""
        project = shell._current_edit_project
        remote_id = resolve_remote_stack_id(project, "a2")
        assert remote_id is not None
        state = versions_state(project, remote_id)
        assert set(state["versions"]) == {DEFAULT_VERSION_NAME, "SharedLook"}
        assert state["default_version"] == DEFAULT_VERSION_NAME
        assert state["active"] == "SharedLook"
        assert state["versions"][DEFAULT_VERSION_NAME][
            "grading_params"]["exposure"] == 3.0  # 当前 live（非空表）
        assert resolve_clip_binding(
            project, "tl1", "c3").domain == VersionDomain.LOCAL


class TestLoad:
    def test_load_same_domain_activates_content(self, env):
        shell, host, strip = env
        _save(shell, "loc-1", "A")                       # 5.0
        _save(shell, "loc-1", "B", exposure=20.0)        # live 20.0
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.LOAD, version_id="A",
                             discard=True)
        assert error == ""
        project = shell._current_edit_project
        assert project.color_stacks["loc-1"].grading_params[
            "exposure"] == 5.0
        assert versions_state(project, "loc-1")["active"] == "A"

    def test_load_without_discard_refused_when_modified(self, env):
        shell, host, strip = env
        project = _save(shell, "loc-1", "A")
        project = _save(shell, "loc-1", "B", exposure=20.0)
        # 修改 live 偏离 active=B
        stack = replace(project.color_stacks["loc-1"],
                        grading_params={"exposure": 33.0})
        project = project.with_color_stacks({**project.color_stacks,
                                             "loc-1": stack})
        shell._current_edit_project = project
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.LOAD, version_id="A",
                             discard=False)
        assert "unsaved" in error.lower()  # UI 前置确认拦截（零提交）
        assert versions_state(
            shell._current_edit_project, "loc-1")["active"] == "B"
        assert shell._current_edit_project.color_stacks["loc-1"] \
            .grading_params["exposure"] == 33.0  # 零提交

    def test_load_cross_domain_switches_reference_and_activates(self, env):
        shell, host, strip = env
        error, _rid = _drive(shell, VersionScope.REMOTE,
                             VersionOperation.CREATE, name="R1")
        assert error == ""
        project = shell._current_edit_project
        remote_id = resolve_remote_stack_id(project, "a1")
        error, _rid = _drive(shell, VersionScope.REMOTE,
                             VersionOperation.LOAD, version_id="R1",
                             discard=True)
        assert error == ""
        project = shell._current_edit_project
        binding = resolve_clip_binding(project, "tl1", "c1")
        assert binding.domain == VersionDomain.REMOTE
        assert binding.stack_id == remote_id
        clip = project.timelines["tl1"].video_tracks[0].clips[0]
        assert clip.color_shared is True
        # WIRE-02：当前会话片段 = c1 → hydrate 新栈；renderer 已更新
        assert shell.hydrate_calls == [remote_id]
        assert shell.renderer_calls and shell.renderer_calls[-1] is project
        assert shell.refresh_calls  # CACHE-01：片段栏重建绑定
        # 回填后的集合反映新绑定 + in_use
        collection = strip.version_ui.collection(
            _target(shell), VersionScope.REMOTE)
        assert collection.in_use and collection.active_version_id == "R1"


class TestTableOnlyOperations:
    def test_rename_does_not_redecode_or_refresh(self, env):
        shell, host, strip = env
        _save(shell, "loc-1", "A")
        _save(shell, "loc-1", "B", exposure=20.0)
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.RENAME, version_id="A",
                             name="Warm")
        assert error == ""
        state = versions_state(shell._current_edit_project, "loc-1")
        assert "Warm" in state["versions"] and "A" not in state["versions"]
        assert not shell.renderer_calls and not shell.refresh_calls
        assert not shell.hydrate_calls  # 仅表操作零重解码

    def test_delete_protections(self, env):
        shell, host, strip = env
        from src.features.edit.core.color_versions import (
            promote_default_version,
        )
        _save(shell, "loc-1", "A")                # 5.0
        _save(shell, "loc-1", "B", exposure=20.0)
        _save(shell, "loc-1", "C", exposure=20.0)
        project, _cmd = promote_default_version(
            shell._current_edit_project, "loc-1")
        shell._current_edit_project = project
        state = versions_state(project, "loc-1")
        default = state["default_version"]
        active = state["active"]
        other = next(n for n in state["versions"]
                     if n not in (default, active))

        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.DELETE, version_id=default)
        assert "protected" in error.lower()  # default=active：双层保护
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.DELETE, version_id=active)
        assert "protected" in error.lower() or "switch" in error.lower()
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.DELETE, version_id=other)
        assert error == ""
        assert other not in versions_state(
            shell._current_edit_project, "loc-1")["versions"]
        assert not shell.renderer_calls and not shell.refresh_calls

    def test_delete_last_refused(self, env):
        shell, host, strip = env
        from src.features.edit.core.color_versions import (
            InitializeDefaultVersionCommand,
        )
        project = InitializeDefaultVersionCommand.create(
            shell._current_edit_project, "loc-1").apply(
            shell._current_edit_project)
        shell._current_edit_project = project
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.DELETE,
                             version_id=DEFAULT_VERSION_NAME)
        assert "protected" in error.lower() or "switch" in error.lower()

    def test_delete_all_loads_default_with_hydrate(self, env):
        shell, host, strip = env
        _save(shell, "loc-1", "A")                # 5.0
        _save(shell, "loc-1", "B", exposure=20.0)
        _save(shell, "loc-1", "C", exposure=20.0)
        # 宿主内建迁移（无 default 键旧表）→ 保留 default 并加载
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.DELETE_ALL)
        assert error == ""
        result_project = shell._current_edit_project
        state = versions_state(result_project, "loc-1")
        assert set(state["versions"]) == {state["default_version"]}
        assert state["active"] == state["default_version"]
        # 迁移提升 active（C=20.0）为 default，加载后 live=C 内容
        assert result_project.color_stacks["loc-1"].grading_params[
            "exposure"] == 20.0
        assert shell.hydrate_calls == ["loc-1"]
        assert shell.refresh_calls


class TestCopyToScope:
    def test_copy_to_empty_remote_initializes_and_duplicates(self, env):
        """c3（素材 a2，无共享栈）作为跨域复制目标：空域初始化 + 复制
        同一宏事务；快照内容来自源版本。"""
        shell, host, strip = env
        _save(shell, "loc-2", "Warm")
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.COPY_TO_SCOPE,
                             version_id="Warm", name="WarmRemote",
                             destination=VersionScope.REMOTE, clip_id="c3")
        assert error == ""
        result = shell._current_edit_project
        remote_id = resolve_remote_stack_id(result, "a2")
        remote_state = versions_state(result, remote_id)
        assert set(remote_state["versions"]) == {
            DEFAULT_VERSION_NAME, "WarmRemote"}
        assert remote_state["versions"]["WarmRemote"]["grading_params"][
            "exposure"] == 3.0  # 来自源快照 Warm（loc-2 live 3.0）
        # 实际使用域不变；目标集合已回填（同一次原子提交）
        assert resolve_clip_binding(
            result, "tl1", "c3").domain == VersionDomain.LOCAL
        remote_collection = strip.version_ui.collection(
            _target(shell, "c3"), VersionScope.REMOTE)
        assert any(v.name == "WarmRemote" for v in remote_collection.versions)

    def test_copy_to_same_scope_refused(self, env):
        shell, host, strip = env
        _save(shell, "loc-1", "Warm")
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.COPY_TO_SCOPE,
                             version_id="Warm", name="X",
                             destination=VersionScope.LOCAL)
        assert error  # UI 前置校验拒绝（另一域）
        assert len(versions_state(
            shell._current_edit_project, "loc-1")["versions"]) == 1


class TestGestureFlush:
    """QA-01 手势 flush 行：保存正确镜头的最后参数；不误保存上一镜头。"""

    def test_flush_does_not_save_previous_clip_stack(self, env):
        """面板正编辑另一镜头的栈（c2 的 rem-1）时对 c1 创建版本：
        不得触发 rem-1 的保存（上一镜头参数不被误存）。"""
        shell, host, strip = env
        shell.grading_panel.current_stack_id = "rem-1"
        shell._grading_save_stack_id = "rem-1"
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.CREATE, name="V1")
        assert error == ""
        assert shell.grading_panel.save_calls == []  # 未误保存上一镜头
        state = versions_state(shell._current_edit_project, "loc-1")
        assert state["versions"]["V1"]["grading_params"][
            "exposure"] == 5.0  # c1 自己的栈内容

    def test_flush_saves_target_stack_pending_gesture(self, env):
        shell, host, strip = env
        shell.grading_panel.current_stack_id = "loc-1"
        shell._grading_save_stack_id = "loc-1"
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.CREATE, name="V1")
        assert error == ""
        assert shell.grading_panel.save_calls == ["loc-1"]  # 目标栈已 flush


class TestPreviewCacheInvalidation:
    """CACHE-01 · 共享依赖、截图归属与失效。"""

    @staticmethod
    def _paint_cache(strip, clip_id):
        from PyQt6.QtGui import QPixmap
        pixmap = QPixmap(8, 8)
        pixmap.fill()
        strip.set_graded_preview(clip_id, pixmap)

    def test_version_load_invalidates_shared_dependents_only(self, env):
        """远程版本加载：引用该共享栈的镜头缓存失效，无关镜头保留；
        活动镜头调度一次新截图（token 护栏由捕获路径负责）。"""
        shell, host, strip = env
        self._paint_cache(strip, "c2")   # c2 在共享栈 rem-1
        self._paint_cache(strip, "c1")   # c1 在本地栈 loc-1（对照）
        error, _rid = _drive(shell, VersionScope.REMOTE,
                             VersionOperation.CREATE, name="R1",
                             clip_id="c2")
        assert error == ""
        self._paint_cache(strip, "c2")   # 截图补上后再切版本
        error, _rid = _drive(shell, VersionScope.REMOTE,
                             VersionOperation.LOAD, version_id="R1",
                             clip_id="c2", discard=True)
        assert error == ""
        assert "c2" not in strip._graded_preview_cache  # 共享栈依赖失效
        assert "c1" in strip._graded_preview_cache       # 无关镜头保留
        # 会话片段是 c1（fixture 默认），本次操作目标是 c2 → 不调度截图
        assert shell.preview_sync_calls == []

    def test_version_load_on_session_clip_schedules_capture(self, env):
        shell, host, strip = env
        _drive(shell, VersionScope.LOCAL, VersionOperation.CREATE,
               name="V1")
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.LOAD, version_id="V1",
                             discard=True)
        assert error == ""
        assert shell.preview_sync_calls == ["c1"]  # 会话片段 == 目标
        assert shell.hydrate_calls == ["loc-1"]

    def test_rename_keeps_cached_previews(self, env):
        """Rename 不改变图像内容：缓存保留，不触发截图/刷新。"""
        shell, host, strip = env
        _save(shell, "loc-1", "A")
        self._paint_cache(strip, "c1")
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.RENAME, version_id="A",
                             name="Warm")
        assert error == ""
        assert "c1" in strip._graded_preview_cache
        assert shell.preview_sync_calls == []
        assert shell.refresh_calls == []


class TestReviewHardening:
    """审查修复回归：C1 信号广播 / S1 内容字段 / S2 单宏迁移 / S3 预校验回滚。"""

    def test_empty_domain_stack_carries_v3_sidebar_fields(self, env):
        """S1：空域建栈的内容复制含 curves_soft_clip/curves_edit（v3）。"""
        from src.app.controllers.edit_controller import (
            V2_SNAPSHOT_CONTENT_FIELDS,
        )
        shell, host, strip = env
        assert "curves_soft_clip" in V2_SNAPSHOT_CONTENT_FIELDS
        assert "curves_edit" in V2_SNAPSHOT_CONTENT_FIELDS
        project = shell._current_edit_project
        from dataclasses import replace as _replace
        enriched = _replace(
            project.color_stacks["loc-2"],
            curves_soft_clip={"soft_strength": 0.5},
            curves_edit={"edit_strength": 0.7})
        shell._current_edit_project = project.with_color_stacks(
            {**project.color_stacks, "loc-2": enriched})
        error, _rid = _drive(shell, VersionScope.REMOTE,
                             VersionOperation.CREATE, name="R",
                             clip_id="c3")
        assert error == ""
        result = shell._current_edit_project
        remote_id = resolve_remote_stack_id(result, "a2")
        created = result.color_stacks[remote_id]
        assert created.curves_soft_clip == {"soft_strength": 0.5}
        assert created.curves_edit == {"edit_strength": 0.7}

    def test_delete_all_migration_is_single_macro_undo(self, env):
        """S2：迁移 + 删除合并单宏——一次 undo 整体恢复（含去掉 default
        标记），命令栈只压一条历史。"""
        shell, host, strip = env
        _save(shell, "loc-1", "A")
        _save(shell, "loc-1", "B", exposure=20.0)
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.DELETE_ALL)
        assert error == ""
        stack = shell.media_pool_panel.get_command_stack()
        assert stack.get_history_size() == 1  # 单宏（非迁移+删除两条）
        undone = stack.undo(shell._current_edit_project)
        state = versions_state(undone, "loc-1")
        # 整体恢复：两版本都在，default 标记回到迁移前（None）
        assert set(state["versions"]) == {"A", "B"}
        assert state["default_version"] is None

    def test_cross_domain_load_prevalidates_payload_zero_commit(self, env,
                                                                monkeypatch):
        """S3：载荷不可重建在切换引用之前拒绝——绑定不变、零提交。"""
        import src.app.controllers.clip_version_host as host_module
        shell, host, strip = env
        error, _rid = _drive(shell, VersionScope.REMOTE,
                             VersionOperation.CREATE, name="R1",
                             clip_id="c3")
        assert error == ""
        project = shell._current_edit_project
        monkeypatch.setattr(host_module, "version_restorable",
                            lambda *a, **k: False)
        error, _rid = _drive(shell, VersionScope.REMOTE,
                             VersionOperation.LOAD, version_id="R1",
                             clip_id="c3", discard=True)
        assert "重建" in error
        assert resolve_clip_binding(
            shell._current_edit_project, "tl1", "c3").domain \
            == VersionDomain.LOCAL  # 未切域

    def test_cross_domain_load_rolls_back_reference_on_activate_failure(
            self, env, monkeypatch):
        """S3：激活失败回滚引用——不留"已换域未加载"半提交。"""
        import src.features.edit.core.color_versions as cv
        shell, host, strip = env
        error, _rid = _drive(shell, VersionScope.REMOTE,
                             VersionOperation.CREATE, name="R1",
                             clip_id="c3")
        assert error == ""
        switches = []
        original_apply = cv.ActivateColorVersionCommand.apply

        def _failing_apply(self_cmd, project):
            switches.append("apply")
            raise ValueError("模拟激活失败")

        monkeypatch.setattr(cv.ActivateColorVersionCommand, "apply",
                            _failing_apply)
        assert original_apply is not None
        error, _rid = _drive(shell, VersionScope.REMOTE,
                             VersionOperation.LOAD, version_id="R1",
                             clip_id="c3", discard=True)
        assert "激活失败" in error or "版本操作失败" in error
        binding = resolve_clip_binding(
            shell._current_edit_project, "tl1", "c3")
        assert binding.domain == VersionDomain.LOCAL  # 引用已回滚
        assert binding.stack_id == "loc-2"


class TestGuardsAndFailures:
    def test_revision_guard_rejects_stale_request(self, env):
        shell, host, strip = env
        target = _target(shell)
        strip.version_ui.state_requested.emit(target, VersionScope.LOCAL)
        collection = strip.version_ui.collection(target, VersionScope.LOCAL)
        assert collection.connected  # 先建立 connected 态
        # 制造 revision 漂移：直接改工程 revision（模拟他处提交）
        project = replace(shell._current_edit_project, revision=99)
        shell._current_edit_project = project
        error, rid = _drive(shell, VersionScope.LOCAL,
                            VersionOperation.CREATE, name="X")
        # _drive 重新读取了集合（revision=99）→ submit 以 99 为基准，
        # 宿主校验一致 → 成功。真正的 stale 需要读取与提交间漂移，
        # 由下方手工请求验证。
        assert rid is not None
        from src.features.color.ui.clip_version_ui import ClipVersionRequest
        from uuid import uuid4
        stale_request = ClipVersionRequest(
            str(uuid4()), target, VersionScope.LOCAL,
            VersionOperation.CREATE, 3, None, "Y", None, False)
        errors = []
        strip.version_ui.operation_finished.connect(
            lambda r: errors.append(r.error))
        strip.version_ui._pending[stale_request.request_id] = stale_request
        host = ClipVersionRequestHost(shell)
        host.execute_operation(stale_request)
        assert errors and ("修订" in errors[0] or "修改" in errors[0])
        assert "Y" not in versions_state(
            shell._current_edit_project, "loc-1")["versions"]

    def test_duplicate_name_zero_commit(self, env):
        shell, host, strip = env
        _save(shell, "loc-1", "Warm")
        before = shell._current_edit_project
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.DUPLICATE, version_id="Warm",
                             name="Warm")
        assert error  # UI 重名校验拒绝
        assert shell._current_edit_project is before

    def test_missing_version_rejected(self, env):
        shell, host, strip = env
        _save(shell, "loc-1", "Warm")
        error, _rid = _drive(shell, VersionScope.LOCAL,
                             VersionOperation.RENAME, version_id="ghost",
                             name="X")
        assert "不存在" in error or "no longer exists" in error

    def test_project_switch_rejects_old_target(self, env):
        shell, host, strip = env
        from src.features.color.ui.clip_version_ui import ClipVersionRequest
        from uuid import uuid4
        target = _target(shell)
        strip.version_ui.state_requested.emit(target, VersionScope.LOCAL)
        request = ClipVersionRequest(
            str(uuid4()), target, VersionScope.LOCAL,
            VersionOperation.CREATE,
            strip.version_ui.collection(
                target, VersionScope.LOCAL).revision,
            None, "X", None, False)
        shell._current_edit_project = replace(
            shell._current_edit_project, project_id="other")
        errors = []
        strip.version_ui.operation_finished.connect(
            lambda r: errors.append(r.error))
        strip.version_ui._pending[request.request_id] = request
        ClipVersionRequestHost(shell).execute_operation(request)
        assert errors and "工程" in errors[0]


class TestSignalWiring:
    def test_strip_state_request_reaches_host(self, env):
        shell, host, strip = env
        target = _target(shell)
        strip.version_state_requested.emit(target, VersionScope.LOCAL)
        collection = strip.version_ui.collection(target, VersionScope.LOCAL)
        assert collection.connected and collection.loaded
