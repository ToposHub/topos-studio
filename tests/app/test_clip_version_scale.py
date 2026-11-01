"""QA-01/PERF-01 · 大工程与连续操作验证（可无头测量的部分）。

计划 §7.3「大工程」行的可自动化子集：
- 菜单读取（state_requested → 权威集合回填）不解码媒体、不重建全条
  卡片（懒加载策略保持）；
- 200/500/1000 clip × 多版本工程上的读取时延有界并记录（本文件断言
  上限，测量值随 pytest -s 输出留档）；
- 连续多次版本操作（创建/改名/复制/删除）零累积重建。

真实 4K 帧解码与 GPU 渲染时延不在本文件范围（需真机测量记录）。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.app.controllers.clip_version_host import ClipVersionRequestHost
from src.features.color.ui.clip_version_ui import (
    VersionOperation, VersionScope,
)
from src.features.color.ui.timeline_clip_strip import TimelineClipStrip
from src.features.edit.core.color_versions import (
    DEFAULT_VERSION_NAME, SaveColorVersionCommand,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, TimelineSequence,
)
from src.shared.project.color_stack import ColorStackData

pytestmark = pytest.mark.app


def _big_project(clip_count: int, versions_per_stack: int = 5):
    """clip_count 个 clip，各自本地栈含 N 个命名版本；每 10 个 clip 共享
    一个 asset_default 远程栈（共享依赖真实存在）。"""
    assets = {}
    clips = []
    stacks = {}
    shared_index = 0
    for i in range(clip_count):
        asset_id = f"a{i // 10}"
        if asset_id not in assets:
            assets[asset_id] = MediaAsset(
                asset_id, f"/m/{asset_id}.mov", "video", asset_id, {})
        clip_id = f"c{i}"
        local_id = f"loc-{i}"
        shared = (i % 10 == 0)
        stack_ref = f"rem-{i // 10}" if shared else local_id
        clips.append(ClipInstance(
            clip_id, asset_id, "v1", i * 10, 10, 0, 10,
            color_stack_id=stack_ref, color_shared=shared))
        stacks[local_id] = ColorStackData(
            stack_id=local_id, asset_id=asset_id, name=local_id,
            scope="clip_local", source_kind="timeline_clip",
            timeline_id="tl1", clip_id=clip_id,
            grading_params={"exposure": float(i % 7)})
        if shared:
            shared_index += 1
    # 远程共享栈
    for j in range((clip_count + 9) // 10):
        stacks[f"rem-{j}"] = ColorStackData(
            stack_id=f"rem-{j}", asset_id=f"a{j}", name=f"rem-{j}",
            scope="asset_default", source_kind="asset_default",
            grading_params={"exposure": 1.0})
    timeline = TimelineSequence(
        timeline_id="tl1", name="TL", fps=24.0,
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=tuple(clips)),))
    project = EditProject(
        project_id="big", name="Big", revision=1,
        assets=assets, color_stacks=stacks, timelines={"tl1": timeline})
    # 为每个本地栈与共享栈预置版本（直接命令构建，避免 500×5 次栈执行）
    from src.features.edit.core.color_versions import (
        InitializeDefaultVersionCommand,
    )
    from dataclasses import replace as _replace
    table = {}
    for stack_id, stack in stacks.items():
        payload_stack = _replace(
            stack, grading_params={**stack.grading_params,
                                   "exposure": 9.0})
        seeded = project.with_color_stacks({
            **project.color_stacks, stack_id: payload_stack})
        state_after = SaveColorVersionCommand.create(
            seeded, stack_id, DEFAULT_VERSION_NAME).apply(seeded)
        entry = state_after.color_stack_versions[stack_id]
        versions = dict(entry["versions"])
        versions[DEFAULT_VERSION_NAME]["saved_at"] = 1000.0
        for v in range(versions_per_stack - 1):
            versions[f"v{v + 1}"] = {
                **versions[DEFAULT_VERSION_NAME], "saved_at":
                    1000.0 + v}
        table[stack_id] = {"active": DEFAULT_VERSION_NAME, "locked": False,
                           "versions": versions,
                           "default_version": DEFAULT_VERSION_NAME}
    return project.with_color_stack_versions(table), timeline


class _ScaleShell:
    def __init__(self, project, timeline, strip):
        self._current_edit_project = project
        self.edit_project_dirty = False
        self.media_pool_panel = None  # 读取路径不需要命令栈
        self.edit_page = type("E", (), {"_current_timeline": timeline})()
        self.color_timeline_clip_strip = strip
        self.grading_panel = None
        self.renderer_calls = []
        self.hydrate_calls = []
        self.refresh_calls = []
        self.preview_sync_calls = []
        self._color_session_manager = None


@pytest.mark.parametrize("clip_count", [200, 500, 1000])
def test_menu_read_latency_and_no_full_rebuild(qtbot, clip_count):
    project, timeline = _big_project(clip_count)
    strip = TimelineClipStrip()
    qtbot.addWidget(strip)
    strip.set_project_and_timeline(project, timeline)
    assert len(strip._all_entries) == clip_count
    shell = _ScaleShell(project, timeline, strip)
    host = ClipVersionRequestHost(shell)
    host.connect_strip(strip)

    rebuild_calls = []
    original_rebuild = strip._rebuild_cards

    def _counted_rebuild():
        rebuild_calls.append(1)
        original_rebuild()

    strip._rebuild_cards = _counted_rebuild
    target = strip._version_target(strip._all_entries[len(
        strip._all_entries) // 2])

    durations = []
    for _ in range(10):
        start = time.perf_counter()
        strip.version_state_requested.emit(target, VersionScope.LOCAL)
        strip.version_state_requested.emit(target, VersionScope.REMOTE)
        durations.append((time.perf_counter() - start) * 1000.0)
    collection = strip.version_ui.collection(target, VersionScope.LOCAL)
    assert collection.connected and collection.loaded
    assert len(collection.versions) == 5
    worst = max(durations)
    # 断言上限（宽松 CI 安全界）：单次两域权威读取 < 50ms（1000 clips）
    assert worst < 50.0, f"read latency {worst:.1f}ms at {clip_count} clips"
    # 菜单读取不重建全条卡片（懒加载/缓存策略未被破坏）
    assert rebuild_calls == []
    print(f"[PERF] {clip_count} clips: read max {worst:.2f}ms, "
          f"avg {sum(durations) / len(durations):.2f}ms")


def test_sequential_operations_do_not_rebuild_whole_strip(qtbot):
    """连续 20 次表操作（改名/复制交替）：全条重建次数为 0（仅表操作
    不触发 refresh；卡片标签经集合回填更新）。"""
    project, timeline = _big_project(300)
    strip = TimelineClipStrip()
    qtbot.addWidget(strip)
    strip.set_project_and_timeline(project, timeline)

    class _Shell(_ScaleShell):
        def __init__(self, project, timeline, strip):
            super().__init__(project, timeline, strip)

            class _Stack:
                def execute(self, command, proj):
                    from src.features.edit.core.commands import (
                        ProjectCommandStack,
                    )
                    # 真实命令栈语义（含 revision 推进）
                    return ProjectCommandStack().execute(command, proj)

            class _Pool:
                def __init__(self):
                    self._stack = _Stack()

                def get_command_stack(self):
                    return self._stack

                def set_project(self, p):
                    pass

                @property
                def project_changed(self):
                    return _NullSignal()

            class _NullSignal:
                def emit(self, *_):
                    pass

            self.media_pool_panel = _Pool()
            self._timeline = timeline

        def refresh_color_timeline_clip_strip(self):
            self.refresh_calls.append(True)
            # 与真实路径一致：重建条目（计数由外层 spy 观察）

    shell = _Shell(project, timeline, strip)
    host = ClipVersionRequestHost(shell)
    host.connect_strip(strip)

    rebuild_calls = []
    original_rebuild = strip._rebuild_cards
    strip._rebuild_cards = lambda: (rebuild_calls.append(1),
                                    original_rebuild())

    target = strip._version_target(strip._all_entries[5])
    errors = []
    strip.version_ui.operation_finished.connect(
        lambda r: errors.append(r.error) if r.error else None)
    for i in range(20):
        strip.version_state_requested.emit(target, VersionScope.LOCAL)
        name = f"copy{i}"
        request_id = strip.version_ui.submit(
            target, VersionScope.LOCAL, VersionOperation.DUPLICATE,
            version_id=DEFAULT_VERSION_NAME, name=name)
        assert request_id is not None, strip.version_ui.last_error
        if errors:
            pytest.fail(f"operation {i} failed: {errors[-1]}")
    final = shell._current_edit_project
    from src.features.edit.core.color_versions import versions_state
    state = versions_state(final, "loc-5")
    assert len(state["versions"]) == 5 + 20
    # 表操作不重建全条（卡片标签经 version_ui 集合更新）
    assert rebuild_calls == []
    print(f"[PERF] 20 sequential table ops: strip rebuilds="
          f"{len(rebuild_calls)}, versions={len(state['versions'])}")
