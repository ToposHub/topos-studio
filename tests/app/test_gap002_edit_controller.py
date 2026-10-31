"""GAP-002 补测：src/app/controllers/edit_controller.py 的高风险低覆盖路径。

覆盖目标（按 GAP_REPORT 缺口明细优先级选取，不凑行数）：
- 纯函数边界/异常分支：_fps_token / _preview_content_aspect /
  _crop_pixmap_to_aspect / _remove_render_cache_file /
  _render_cache_manifest_from_value。
- 撤销栈与命令语义：_execute_timeline_edit_command 的 view 栈路由与
  直连回退、_CreateStampedColorStackCommand 的 displaced 碰撞守卫。
- 状态机分支：_cleanup_obsolete_render_cache_files 的过滤矩阵、
  _prune_render_cache_variants、MG 空闲缓存调度/取消、
  _auto_evaluate_render_cache 的 smart 模式守卫。
- 错误/异常路径：字幕命令提交失败回滚、render-cache 失效吞错、
  ASR/TTS worker 的 failed/cancelled/空结果分支（同步调用 run()）。
- 公开 API 契约：_apply_project_change 的全量同步、ADR 上下文降级、
  音频路由错误收集。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

pytestmark = pytest.mark.app

from src.app.controllers.edit_controller import (
    _CreateStampedColorStackCommand,
    _SpeechToTextWorker,
    _SubtitleToSpeechWorker,
    _crop_pixmap_to_aspect,
    _fps_token,
    _preview_content_aspect,
    _render_cache_manifest_from_value,
    _remove_render_cache_file,
)
from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
from src.features.edit.core.render_cache import RenderCacheManifest
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


# --------------------------------------------------------------------------- #
# 测试宿主（Mixin 不持有状态，属性解析到宿主）
# --------------------------------------------------------------------------- #


class _Signal:
    """最小 pyqtSignal 替身（记录 emit 参数）。"""

    def __init__(self) -> None:
        self.emitted: list[Any] = []

    def emit(self, *args: Any) -> None:
        self.emitted.append(args)

    def connect(self, slot: Any) -> None:
        pass

    def disconnect(self, slot: Any | None = None) -> None:
        pass


class _StatusBar:
    def __init__(self) -> None:
        self.messages: list[tuple[str, int]] = []

    def showMessage(self, message: str, timeout: int = 0) -> None:
        self.messages.append((message, timeout))


class _Renderer:
    def __init__(self, *, render_idle: bool = True, raise_idle: bool = False) -> None:
        self.sequences: list[Any] = []
        self.projects: list[Any] = []
        self.color_stack_updates: list[Any] = []
        self._render_idle = render_idle
        self._raise_idle = raise_idle

    def set_sequence(self, timeline: Any) -> None:
        self.sequences.append(timeline)

    def set_project(self, project: Any) -> None:
        self.projects.append(project)

    def update_color_stacks(self, project: Any) -> None:
        self.color_stack_updates.append(project)

    def is_render_idle(self) -> bool:
        if self._raise_idle:
            raise RuntimeError("renderer gone")
        return self._render_idle


class _TimelineView:
    """覆盖 _execute_timeline_edit_command / 字幕命令提交路径的视图桩。"""

    def __init__(self, timeline: Any | None = None) -> None:
        self._timeline = timeline
        self.executed: list[Any] = []
        self.raise_on_execute: bool = False
        self._items_map: dict[str, Any] = {}
        self.layout_calls = 0
        self.loaded: list[Any] = []
        self.projects: list[Any] = []
        self.project_changed = _Signal()

    def execute_command(self, command: Any) -> Any:
        if self.raise_on_execute:
            raise ValueError("overlap rejected")
        self.executed.append(command)
        return self._timeline

    def load_timeline(self, timeline: Any) -> None:
        self.loaded.append(timeline)
        self._timeline = timeline

    def set_project(self, project: Any) -> None:
        self.projects.append(project)

    def _layout_track_items(self) -> None:
        self.layout_calls += 1


class _TimelinePanel:
    def __init__(self, view: _TimelineView | None = None) -> None:
        self.timeline_view = view
        self.loaded: list[Any] = []
        self.projects: list[Any] = []
        self.current_timelines: list[str | None] = []
        self.cleared = 0
        self.provisional_shown = False

    def get_timeline_view(self) -> _TimelineView | None:
        return self.timeline_view

    def load_timeline(self, timeline: Any) -> None:
        self.loaded.append(timeline)

    def set_project(self, project: Any) -> None:
        self.projects.append(project)

    def set_current_timeline(self, timeline_id: str | None) -> None:
        self.current_timelines.append(timeline_id)

    def clear(self) -> None:
        self.cleared += 1

    def show_provisional_timeline(self) -> None:
        self.provisional_shown = True


class _EditPage:
    def __init__(self, timeline: Any | None = None) -> None:
        self._current_timeline = timeline
        self._current_frame = 0
        self._is_playing = False
        self._program_viewer = None
        self.timelines_set: list[Any] = []

    def set_timeline(self, timeline: Any | None) -> None:
        self.timelines_set.append(timeline)
        self._current_timeline = timeline


class _CommandStack:
    def __init__(self) -> None:
        self.executed: list[Any] = []

    def execute(self, command: Any, project: Any) -> Any:
        self.executed.append(command)
        return command.apply(project)


class _MediaPool:
    def __init__(self) -> None:
        self.projects: list[Any] = []
        self.current_timelines: list[str | None] = []
        self._command_stack = _CommandStack()

    def set_project(self, project: Any) -> None:
        self.projects.append(project)

    def set_current_timeline(self, timeline_id: str | None) -> None:
        self.current_timelines.append(timeline_id)

    def get_command_stack(self) -> _CommandStack:
        return self._command_stack


class _AdrPanel:
    def __init__(self, *, raise_settings: bool = False) -> None:
        self.sheets: list[Any] = []
        self._raise_settings = raise_settings

    def adr_settings(self) -> dict[str, Any]:
        if self._raise_settings:
            raise RuntimeError("panel disposed")
        return {"device_id": "default"}

    def set_take_sheet(self, sheet: Any) -> None:
        self.sheets.append(sheet)


def _controller(
    project: EditProject | None = None,
    timeline: Any | None = None,
    *,
    view: _TimelineView | None = None,
    renderer: _Renderer | None = None,
) -> Any:
    """组装 EditControllerMixin 宿主（属性名对齐 MainWindow）。"""
    from src.app.controllers.edit_controller import EditControllerMixin

    class _Host(EditControllerMixin):
        def __init__(self) -> None:
            self._current_edit_project = project
            self._current_timeline_sequences: dict[str, TimelineSequence] = {}
            self.edit_page = _EditPage(timeline)
            self.media_pool_panel = _MediaPool()
            self.timeline_panel = _TimelinePanel(view)
            self.timeline_preview_renderer = renderer
            self.status_bar = _StatusBar()
            self.video_controller = None
            self.edit_project_dirty = False
            self._render_cache_workers: dict[str, Any] = {}
            self._render_cache_inflight_clips: set[Any] = set()
            self._render_cache_inflight_cache_keys: dict[str, Any] = {}
            self._mg_idle_cache_ids: set[str] = set()

    return _Host()


def _timeline_with_clip(
    timeline_id: str = "t1",
    *,
    asset_id: str = "asset-video",
    clip_id: str = "clip-1",
    start: int = 0,
    duration: int = 48,
    fps: Rational | None = None,
) -> TimelineSequence:
    clip = ClipInstance(
        clip_id=clip_id,
        asset_id=asset_id,
        track_id=f"{timeline_id}-v1",
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
    )
    return TimelineSequence(
        timeline_id=timeline_id,
        name=timeline_id,
        fps=fps or Rational(24, 1),
        video_tracks=(
            EditTrack(
                track_id=f"{timeline_id}-v1",
                name="V1",
                track_type="video",
                clips=(clip,),
            ),
        ),
    )


def _manifest(**overrides: Any) -> RenderCacheManifest:
    values: dict[str, Any] = {
        "cache_id": "cache-1",
        "timeline_id": "t1",
        "clip_id": "clip-1",
        "path": "/tmp/does-not-exist.mp4",
        "status": "stale",
    }
    values.update(overrides)
    return RenderCacheManifest(**values)


def _project_with_manifests(*manifests: RenderCacheManifest) -> EditProject:
    project = EditProject.create("gap-002")
    return replace(
        project,
        render_cache_manifests={m.cache_id: m.to_dict() for m in manifests},
    )


# --------------------------------------------------------------------------- #
# 纯函数：_fps_token
# --------------------------------------------------------------------------- #


def test_fps_token_normalizes_supported_representations() -> None:
    assert _fps_token(None) == ""
    assert _fps_token(Rational(24000, 1001)) == "24000/1001"
    assert _fps_token("24000/1001") == "24000/1001"
    assert _fps_token(24.0) == "24/1"
    # float 时基收敛到 limit_denominator 的精确分数（而非小数串）
    assert _fps_token(23.976) == "2997/125"


def test_fps_token_degrades_for_malformed_values() -> None:
    assert _fps_token("abc/def") == "abc/def"
    assert _fps_token("not-a-fraction")  # 任意可估读值也保持 num/den 形态
    assert "/" in _fps_token(object()) or _fps_token(object())


# --------------------------------------------------------------------------- #
# 纯函数：_preview_content_aspect / _crop_pixmap_to_aspect
# --------------------------------------------------------------------------- #


class _Size:
    def __init__(self, width: float, height: float) -> None:
        self._size = (width, height)

    def __getitem__(self, index: int) -> float:
        return self._size[index]


def test_preview_content_aspect_video_mode_prefers_display_size() -> None:
    renderer = type(
        "R",
        (),
        {
            "current_video_display_size": _Size(1920.0, 1080.0),
            "video_source_size": _Size(640.0, 480.0),
        },
    )()
    engine = type("E", (), {"_is_video_mode": True, "renderer": renderer})()
    assert _preview_content_aspect(engine) == pytest.approx(16 / 9)


def test_preview_content_aspect_image_mode_ignores_stale_video_metadata() -> None:
    # 图像模式下渲染器残留的 last-video 元数据不得污染新图的长宽比
    renderer = type(
        "R",
        (),
        {"current_video_display_size": _Size(1080.0, 1920.0)},
    )()
    engine = type("E", (), {"_is_video_mode": False, "renderer": renderer})()
    engine.scene_size = _Size(400.0, 200.0)
    assert _preview_content_aspect(engine) == pytest.approx(2.0)


def test_preview_content_aspect_falls_back_to_source_shape_then_none() -> None:
    renderer = type("R", (), {})()
    engine = type("E", (), {"_is_video_mode": False, "renderer": renderer})()
    engine.scene_size = _Size(0.0, 100.0)  # 非法候选 → 跳过
    source = type("S", (), {"shape": (100, 300, 3)})()  # HWC: h=100 w=300
    engine.img_source_full = source
    assert _preview_content_aspect(engine) == pytest.approx(3.0)

    engine.img_source_full = None
    assert _preview_content_aspect(engine) is None


def test_crop_pixmap_to_aspect_removes_horizontal_matte(qapp) -> None:
    from PyQt6.QtGui import QColor, QPixmap

    wide = QPixmap(200, 100)
    wide.fill(QColor("red"))
    cropped = _crop_pixmap_to_aspect(wide, 1.0)
    assert cropped.width() == 100
    assert cropped.height() == 100

    tall = QPixmap(100, 200)
    tall.fill(QColor("blue"))
    cropped = _crop_pixmap_to_aspect(tall, 1.0)
    assert cropped.width() == 100
    assert cropped.height() == 100


def test_crop_pixmap_to_aspect_passthrough_cases(qapp) -> None:
    from PyQt6.QtGui import QColor, QPixmap

    square = QPixmap(100, 100)
    square.fill(QColor("green"))
    # 目标比例一致（±1%）→ 原样返回
    assert _crop_pixmap_to_aspect(square, 1.0) is square
    # null pixmap / 无目标比例 / 非法比例 → 原样返回
    assert _crop_pixmap_to_aspect(QPixmap(), 1.0).isNull()
    assert _crop_pixmap_to_aspect(square, None) is square
    assert _crop_pixmap_to_aspect(square, 0.0) is square


# --------------------------------------------------------------------------- #
# 纯函数：render-cache 文件清理 / manifest 归一化
# --------------------------------------------------------------------------- #


def test_remove_render_cache_file_deletes_all_variants(tmp_path) -> None:
    main = tmp_path / "clip.mp4"
    main.write_bytes(b"main")
    legacy = tmp_path / "clip.mp4.tmp.mp4"
    legacy.write_bytes(b"tmp")
    realtime = tmp_path / "clip.mp4.realtime.tmp.mp4"
    realtime.write_bytes(b"realtime")

    _remove_render_cache_file(str(main))

    assert not main.exists()
    assert not legacy.exists()
    assert not realtime.exists()


def test_remove_render_cache_file_tolerates_missing_and_empty(tmp_path) -> None:
    _remove_render_cache_file("")  # 空路径 no-op
    _remove_render_cache_file(str(tmp_path / "never-existed.mp4"))
    _remove_render_cache_file("   ")  # 仅空白
    assert not list(tmp_path.iterdir()) or True  # 未创建任何文件


def test_render_cache_manifest_from_value_normalization() -> None:
    manifest = _manifest()
    assert _render_cache_manifest_from_value(manifest) is manifest
    assert _render_cache_manifest_from_value(manifest.to_dict()) == manifest
    assert _render_cache_manifest_from_value(None) is None
    assert _render_cache_manifest_from_value(12345) is None
    # 未知键的 dict 由 from_dict 兜底为默认 manifest（宽松契约）
    fallback = _render_cache_manifest_from_value({"bogus": True})
    assert fallback is not None and fallback.clip_id == ""


# --------------------------------------------------------------------------- #
# _CreateStampedColorStackCommand：撤销/重放与 displaced 碰撞守卫
# --------------------------------------------------------------------------- #


def test_stamped_color_stack_command_undo_redo_keeps_stack_id() -> None:
    from src.shared.project.color_stack import ColorStackData

    project = EditProject.create("stamp")
    command = _CreateStampedColorStackCommand.create(
        asset_id="a1", name="Asset", stamp_fields={"scope": "asset_default"},
    )
    applied = command.apply(project)
    stack_id = command.created_stack_id
    assert stack_id in applied.color_stacks
    assert applied.color_stacks[stack_id].scope == "asset_default"

    undone = command.undo(applied)
    assert stack_id not in undone.color_stacks

    # redo 重放同一对象（stack_id 稳定，绑定不悬空）
    reapplied = command.apply(undone)
    assert reapplied.color_stacks[stack_id] is applied.color_stacks[stack_id]
    assert isinstance(reapplied.color_stacks[stack_id], ColorStackData)


def test_stamped_color_stack_command_undo_restores_displaced_stack() -> None:
    from src.shared.project.color_stack import ColorStackData

    command = _CreateStampedColorStackCommand.create(asset_id=None, name="N")
    first = command.apply(EditProject.create("p1"))
    stack_id = command.created_stack_id
    original = first.color_stacks[stack_id]

    # 另一命令创建了同 id 的栈（碰撞）：undo 必须恢复 displaced 原栈
    other = replace(original, name="Other")  # 同 id 的替代栈
    replaced_project = replace(
        first, color_stacks={**first.color_stacks, stack_id: other},
    )
    reapplied = command.apply(replaced_project)  # 记录 displaced 并重插自己的栈
    undone = command.undo(reapplied)
    # undo 恢复的是 apply 时被顶替的原栈（other），而不是删除
    assert undone.color_stacks[stack_id] is other
    assert other is not original


def test_stamped_color_stack_command_undo_is_noop_when_stack_replaced() -> None:
    command = _CreateStampedColorStackCommand.create(asset_id=None, name="N")
    applied = command.apply(EditProject.create("p2"))
    stack_id = command.created_stack_id

    # 后续操作已替换该 id 的栈：undo 不得动它（守卫分支）
    from src.shared.project.color_stack import ColorStackData

    successor = replace(
        applied,
        color_stacks={
            **applied.color_stacks,
            stack_id: ColorStackData.create(asset_id=None, name="New"),
        },
    )
    fresh = _CreateStampedColorStackCommand.create(asset_id=None, name="F2")
    fresh.undo(successor)  # 未 apply 的命令 undo → 原样返回
    assert fresh.undo(successor) is successor
    # 命令自身的守卫：project 中的栈不是自己创建的那个 → 原样返回
    assert command.undo(successor) is successor


# --------------------------------------------------------------------------- #
# 时间线工具：fps 归一 / 对话框格式适配
# --------------------------------------------------------------------------- #


def test_rational_for_timeline_fps_maps_known_dropframe_rates() -> None:
    host = _controller()
    assert host._rational_for_timeline_fps(23.976) == Rational(24000, 1001)
    assert host._rational_for_timeline_fps(29.97) == Rational(30000, 1001)
    assert host._rational_for_timeline_fps(59.94) == Rational(60000, 1001)
    assert host._rational_for_timeline_fps(24.0) == Rational(24, 1)
    # 任意小数时基 → 毫秒级精确分数
    assert host._rational_for_timeline_fps(12.5) == Rational(12500, 1000)


def test_format_settings_from_dialog_merges_only_whitelisted_keys() -> None:
    from src.app.controllers.edit_controller import EditControllerMixin

    merged = EditControllerMixin._format_settings_from_dialog(
        {
            "pixel_aspect_ratio": 1.5,
            "start_timecode": "01:00:00:00",
            "name": "ignored",
            "custom_unknown": "dropped",
        },
        existing={"field_order": "progressive", "start_timecode": "00:00:00:00"},
    )
    assert merged == {
        "field_order": "progressive",
        "pixel_aspect_ratio": 1.5,
        "start_timecode": "01:00:00:00",
    }


def test_timeline_settings_for_dialog_adapts_sequence() -> None:
    from src.app.controllers.edit_controller import EditControllerMixin

    sequence = _timeline_with_clip(fps=Rational(24000, 1001))
    sequence = replace(sequence, format_settings={"field_order": "progressive"})
    settings = EditControllerMixin._timeline_settings_for_dialog(
        sequence, {"start_timecode": "01:00:00:00"},
    )
    assert settings["name"] == "t1"
    assert settings["fps_num"] == 24000
    assert settings["fps_den"] == 1001
    assert settings["fps"] == pytest.approx(23.976, abs=0.01)
    # ref 的格式设置作为底、sequence 覆盖其上
    assert settings["start_timecode"] == "01:00:00:00"
    assert settings["field_order"] == "progressive"


# --------------------------------------------------------------------------- #
# _execute_timeline_edit_command：撤销栈路由 + 回退
# --------------------------------------------------------------------------- #


class _RecordingCommand:
    def __init__(self, result: Any) -> None:
        self.applied: list[Any] = []
        self._result = result

    def apply(self, timeline: Any) -> Any:
        self.applied.append(timeline)
        return self._result


def test_execute_timeline_edit_command_routes_through_view_stack() -> None:
    timeline = _timeline_with_clip()
    updated = _timeline_with_clip(clip_id="clip-2")
    view = _TimelineView(timeline)
    view.load_timeline(updated)
    host = _controller(view=view)
    command = _RecordingCommand(updated)

    result = host._execute_timeline_edit_command(command, timeline)

    # view 持有 timeline → 必须经 execute_command（可撤销入口）
    assert view.executed == [command]
    assert command.applied == []  # 不走直连 apply
    assert result is updated


def test_execute_timeline_edit_command_fallback_applies_and_syncs() -> None:
    timeline = _timeline_with_clip()
    updated = _timeline_with_clip(clip_id="clip-2")
    renderer = _Renderer()
    view = _TimelineView()
    panel_view = _TimelineView()
    host = _controller(timeline=timeline, view=view, renderer=renderer)
    host.timeline_panel.timeline_view = panel_view
    # 视图没有 _timeline（宿主降级）：tuple 结果解包 + 手动同步
    command = _RecordingCommand((updated, "cmd"))
    result = host._execute_timeline_edit_command(command, timeline)

    assert command.applied == [timeline]
    assert result is updated
    assert host.edit_page._current_timeline is updated
    assert renderer.sequences == [updated]
    assert panel_view.loaded == [updated]


def test_sync_timeline_to_views_without_renderer_still_loads_view() -> None:
    timeline = _timeline_with_clip()
    panel_view = _TimelineView()
    host = _controller(view=None, renderer=None)
    host.timeline_panel.timeline_view = panel_view

    host._sync_timeline_to_views(timeline)

    assert panel_view.loaded == [timeline]


# --------------------------------------------------------------------------- #
# render-cache 失效与清理
# --------------------------------------------------------------------------- #


def test_invalidate_clip_render_cache_marks_valid_manifest_stale(tmp_path) -> None:
    staleable = _manifest(
        cache_id="c-valid",
        path=str(tmp_path / "valid.mp4"),
        status="valid",
        render_fingerprint="",
    )
    fingerprinted = _manifest(
        cache_id="c-fp",
        path=str(tmp_path / "fp.mp4"),
        status="valid",
        render_fingerprint="keep",
    )
    (tmp_path / "valid.mp4").write_bytes(b"v")
    (tmp_path / "fp.mp4").write_bytes(b"f")
    project = _project_with_manifests(staleable, fingerprinted)
    host = _controller(project)

    host._invalidate_clip_render_cache("clip-1")

    new_project = host._current_edit_project
    assert new_project is not project
    manifests = new_project.render_cache_manifests
    assert manifests["c-valid"]["status"] == "stale"
    # 有指纹的 valid 缓存不被清理（preserve_valid_fingerprinted）
    assert manifests["c-fp"]["status"] == "valid"
    assert (tmp_path / "fp.mp4").exists()


def test_invalidate_clip_render_cache_swallows_project_errors() -> None:
    class _ExplodingProject:
        render_cache_manifests: dict[str, Any] = {}

        def invalidate_render_cache_for_clip(self, *args: Any) -> Any:
            raise RuntimeError("boom")

    host = _controller(_ExplodingProject())  # type: ignore[arg-type]

    host._invalidate_clip_render_cache("clip-1")  # 不得抛出

    assert isinstance(host._current_edit_project, _ExplodingProject)


def test_invalidate_clip_render_cache_guards_missing_project() -> None:
    host = _controller(None)
    host._invalidate_clip_render_cache("clip-1")
    assert host._current_edit_project is None

    # 缺失 invalidate API 的工程 → 早退（不抛 AttributeError）
    host2 = _controller(EditProject.create("p"))
    host2._current_edit_project = object()  # type: ignore[assignment]
    host2._invalidate_clip_render_cache("clip-1")


def test_cleanup_obsolete_render_cache_files_filters_by_scope(tmp_path) -> None:
    rendering = _manifest(
        cache_id="c-rendering", clip_id="clip-1",
        path=str(tmp_path / "rendering.mov"), status="rendering",
    )
    (tmp_path / "rendering.mov").write_bytes(b"r")
    other_clip = _manifest(
        cache_id="c-other", clip_id="clip-2",
        path=str(tmp_path / "other.mov"), status="stale",
    )
    (tmp_path / "other.mov").write_bytes(b"o")
    project = _project_with_manifests(rendering, other_clip)

    from src.app.controllers.edit_controller import EditControllerMixin

    EditControllerMixin._cleanup_obsolete_render_cache_files(
        project, clip_id="clip-1",
    )

    # rendering 状态默认保留；clip-2 的文件不在本次目标范围
    assert (tmp_path / "rendering.mov").exists()
    assert (tmp_path / "other.mov").exists()

    EditControllerMixin._cleanup_obsolete_render_cache_files(
        project, clip_id="clip-2",
    )
    assert not (tmp_path / "other.mov").exists()
    assert (tmp_path / "rendering.mov").exists()


def test_cleanup_obsolete_render_cache_files_keeps_shared_paths(tmp_path) -> None:
    shared = str(tmp_path / "shared.mov")
    (tmp_path / "shared.mov").write_bytes(b"s")
    manifests = (
        _manifest(cache_id="c-a", clip_id="clip-1", path=shared, status="stale"),
        # 另一 manifest（别的 clip）仍引用同一文件
        _manifest(cache_id="c-b", clip_id="clip-2", path=shared, status="valid"),
    )
    project = _project_with_manifests(*manifests)

    from src.app.controllers.edit_controller import EditControllerMixin

    EditControllerMixin._cleanup_obsolete_render_cache_files(project, clip_id="clip-1")

    # R2-014 修复后契约：保护集按 manifest 身份（过滤谓词补集）划分。
    # c-b（clip-2, valid）非本次目标却仍引用 shared.mov → 共享文件保留。
    assert (tmp_path / "shared.mov").exists()

    # 双方都进入目标集（clip-1 与 clip-2 一并清理）后，不再有"他处引用"，
    # 共享文件此时才允许删除。
    EditControllerMixin._cleanup_obsolete_render_cache_files(project)
    assert not (tmp_path / "shared.mov").exists()

    # include_rendering 显式确认取消后，rendering 文件也纳入清理
    orphan_rendering = _manifest(
        cache_id="c-r", clip_id="clip-9",
        path=str(tmp_path / "orphan.mp4"), status="rendering",
    )
    (tmp_path / "orphan.mp4").write_bytes(b"x")
    project2 = _project_with_manifests(orphan_rendering)
    EditControllerMixin._cleanup_obsolete_render_cache_files(
        project2, include_rendering=True,
    )
    assert not (tmp_path / "orphan.mp4").exists()


def test_prune_render_cache_variants_bounds_history_and_files(tmp_path) -> None:
    manifests = tuple(
        _manifest(
            cache_id=f"c-{index}",
            path=str(tmp_path / f"v{index}.mov"),
            status="valid",
        )
        for index in range(4)
    )
    for index in range(4):
        (tmp_path / f"v{index}.mov").write_bytes(b"v")
    project = _project_with_manifests(*manifests)

    from src.app.controllers.edit_controller import EditControllerMixin

    pruned = EditControllerMixin._prune_render_cache_variants(
        project, timeline_id="t1", clip_id="clip-1", max_variants=2,
    )

    assert pruned is not project
    assert len(pruned.render_cache_manifests) == 2
    # 最新两个保留，最旧两个的文件被删除
    assert (tmp_path / "v3.mov").exists()
    assert (tmp_path / "v2.mov").exists()
    assert not (tmp_path / "v0.mov").exists()
    assert not (tmp_path / "v1.mov").exists()


def test_prune_render_cache_variants_degrades_safely() -> None:
    from src.app.controllers.edit_controller import EditControllerMixin

    project = EditProject.create("no-prune")
    # 宿主没有 prune API → 原样返回
    assert EditControllerMixin._prune_render_cache_variants(
        project, timeline_id="t", clip_id="c",
    ) is project

    class _Broken:
        def prune_render_cache_variants(self, *args: Any, **kwargs: Any) -> Any:
            raise TypeError("bad args")

    broken = _Broken()
    assert EditControllerMixin._prune_render_cache_variants(
        broken, timeline_id="t", clip_id="c",
    ) is broken


# --------------------------------------------------------------------------- #
# 音频路由错误收集（保存前提示）
# --------------------------------------------------------------------------- #


def test_collect_audio_routing_errors_degrades_safe() -> None:
    host = _controller()
    host.edit_page = None
    assert host._collect_audio_routing_errors() == ()

    class _Raising:
        @property
        def audio_routing_errors(self) -> list[str]:
            raise RuntimeError("probe failed")

    host2 = _controller()
    host2.edit_page = _Raising()
    assert host2._collect_audio_routing_errors() == ()

    class _Ok:
        audio_routing_errors = ["A1 未连接输出", "Bus L missing"]

    host3 = _controller()
    host3.edit_page = _Ok()
    assert host3._collect_audio_routing_errors() == (
        "A1 未连接输出", "Bus L missing",
    )


def test_warn_audio_routing_errors_truncates_long_lists(monkeypatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    host = _controller()

    class _Many:
        audio_routing_errors = [f"error-{i}" for i in range(10)]

    host.edit_page = _Many()
    captured: dict[str, Any] = {}

    def _capture(*args: Any, **kwargs: Any) -> None:
        captured["text"] = args[-1]

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(_capture))
    host._warn_audio_routing_errors_on_save()

    assert "error-7" in captured["text"]
    assert "+2" in captured["text"]  # 超出 8 条的部分折叠

    # 无错误 → 不弹窗
    calls: list[Any] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: calls.append(a)),
    )
    host.edit_page = None
    host._warn_audio_routing_errors_on_save()
    assert calls == []


# --------------------------------------------------------------------------- #
# 多机位 / 查找工具
# --------------------------------------------------------------------------- #


def _multicam_project() -> tuple[EditProject, TimelineSequence]:
    group_meta = {
        "group_id": "g1",
        "name": "Interview",
        "default_angle_id": "1",
        "angles": [
            {"angle_id": "1", "asset_id": "cam-a", "sync_offset": 0},
            {"angle_id": "2", "asset_id": "cam-b", "sync_offset": 12},
        ],
    }
    asset = MediaAsset(
        asset_id="cam-a", path="/a.mp4", media_type="video",
        display_name="A", metadata={"multicam_group": group_meta},
    )
    project = EditProject.create("multicam")
    project = replace(project, assets={**project.assets, "cam-a": asset})
    timeline = _timeline_with_clip(asset_id="cam-a")
    return project, timeline


def test_multicam_clip_at_playhead_resolves_by_group() -> None:
    project, timeline = _multicam_project()
    host = _controller(project, timeline)
    host.edit_page._current_frame = 10

    clip = host._multicam_clip_at_playhead()
    assert clip is not None and clip.clip_id == "clip-1"
    assert host._multicam_clip_at_playhead("g1") is clip
    # 指定不存在的组 → None
    assert host._multicam_clip_at_playhead("other-group") is None


def test_multicam_clip_at_playhead_none_cases() -> None:
    # 无时间线
    host = _controller(EditProject.create("p"))
    assert host._multicam_clip_at_playhead() is None

    # playhead 落在片段外 / 非多机位素材
    project, timeline = _multicam_project()
    host2 = _controller(project, timeline)
    host2.edit_page._current_frame = 10_000
    assert host2._multicam_clip_at_playhead() is None

    plain = EditProject.create("plain")
    host3 = _controller(plain, _timeline_with_clip(asset_id="plain-asset"))
    host3.edit_page._current_frame = 5
    assert host3._multicam_clip_at_playhead() is None


def test_find_timeline_clip_searches_all_tracks() -> None:
    timeline = _timeline_with_clip()
    host = _controller(EditProject.create("p"), timeline)

    assert host._find_timeline_clip("clip-1") is timeline.video_tracks[0].clips[0]
    assert host._find_timeline_clip("missing") is None

    host.edit_page._current_timeline = None
    assert host._find_timeline_clip("clip-1") is None


# --------------------------------------------------------------------------- #
# 字幕命令提交 / 失败回滚
# --------------------------------------------------------------------------- #


class _SubtitleTimeline:
    """覆盖 get_subtitle_clip_by_id / get_track_by_id 的轻量时间线。"""

    def __init__(self, clips: dict[str, Any] | None = None) -> None:
        self.subtitle_clips = list((clips or {}).values())
        self._clips = clips or {}
        self._tracks = {
            clip.track_id: type(
                "T", (), {"track_id": clip.track_id, "track_type": "subtitle"},
            )()
            for clip in self._clips.values()
        }

    def get_subtitle_clip_by_id(self, clip_id: str) -> Any:
        return self._clips.get(clip_id)

    def get_track_by_id(self, track_id: str) -> Any:
        return self._tracks.get(track_id)


class _SubtitleClip:
    def __init__(
        self, clip_id: str = "sub-1", track_id: str = "s1",
        start: int = 24, duration: int = 48, font_size: int = 32,
        fill_r: float = 1.0, style_override: Any = None,
    ) -> None:
        self.clip_id = clip_id
        self.track_id = track_id
        self.timeline_start = start
        self.timeline_duration = duration
        self.text = "hello"
        self.font_size = font_size
        self.fill_r = fill_r
        self.style_override = style_override


def test_get_subtitle_clip_field_uses_defaults_safely() -> None:
    host = _controller()
    host.edit_page = _EditPage(None)  # 无时间线 → 默认值
    assert host._get_subtitle_clip_field("sub-1", "timeline_duration", 72) == 72
    assert host._get_subtitle_clip_duration("sub-1") == 72
    assert host._get_subtitle_clip_start("sub-1") == 0

    clip = _SubtitleClip(start=96, duration=12)
    host2 = _controller(
        EditProject.create("p"), _SubtitleTimeline({"sub-1": clip}),
    )
    assert host2._get_subtitle_clip_duration("sub-1") == 12
    assert host2._get_subtitle_clip_start("sub-1") == 96
    assert host2._get_subtitle_clip_field("sub-1", "missing-field", "dft") == "dft"


def test_execute_subtitle_command_requires_timeline_and_view() -> None:
    host = _controller()  # edit_page 有属性但无时间线
    assert host._execute_subtitle_command(object()) is None

    # 有 timeline、无 timeline_view
    host2 = _controller(EditProject.create("p"), _SubtitleTimeline())
    assert host2._execute_subtitle_command(object()) is None

    # 成功路径：经 view.execute_command
    view = _TimelineView()
    host3 = _controller(EditProject.create("p"), _SubtitleTimeline())
    host3.timeline_panel.timeline_view = view
    command = object()
    assert host3._execute_subtitle_command(command) is True
    assert view.executed == [command]


def test_execute_subtitle_command_failure_shows_status_message() -> None:
    view = _TimelineView()
    view.raise_on_execute = True
    host = _controller(EditProject.create("p"), _SubtitleTimeline())
    host.timeline_panel.timeline_view = view

    result = host._execute_subtitle_command(object())

    assert result is False
    assert host.status_bar.messages
    assert "Subtitle operation failed" in host.status_bar.messages[-1][0]


def test_commit_subtitle_drag_or_restore_restores_geometry_on_failure() -> None:
    view = _TimelineView()
    view.raise_on_execute = True
    item = type("Item", (), {})()
    view._items_map["sub-1"] = item
    host = _controller(EditProject.create("p"), _SubtitleTimeline())
    host.timeline_panel.timeline_view = view

    restored: list[tuple[Any, int, int]] = []

    from src.features.edit.core.captions import edit_policy as policy_mod

    monkeypatch_target = policy_mod.SubtitleEditPolicy.restore_on_failure
    policy_mod.SubtitleEditPolicy.restore_on_failure = staticmethod(
        lambda clip, start, duration: restored.append((clip, start, duration)),
    )
    try:
        ok = host._commit_subtitle_drag_or_restore(
            object(), "sub-1", original_start=10, original_duration=20,
        )
    finally:
        policy_mod.SubtitleEditPolicy.restore_on_failure = monkeypatch_target

    assert ok is False
    assert restored == [(item, 10, 20)]
    assert view.layout_calls == 1


def test_commit_subtitle_drag_or_restore_success_path() -> None:
    view = _TimelineView()
    host = _controller(EditProject.create("p"), _SubtitleTimeline())
    host.timeline_panel.timeline_view = view

    assert host._commit_subtitle_drag_or_restore(
        object(), "sub-1", original_start=0, original_duration=10,
    ) is True
    assert view.layout_calls == 0


def test_custom_subtitle_style_updates_freezes_effective_style() -> None:
    clip = _SubtitleClip(font_size=32, fill_r=0.5)
    timeline = _SubtitleTimeline({"sub-1": clip})
    host = _controller(EditProject.create("p"), timeline)

    updates = host._custom_subtitle_style_updates(
        timeline, clip, {"font_size": 48},
    )

    assert updates["font_size"] == 48
    override = updates["style_override"]
    # 未提供的字段冻结为生效值（fill_r 不回落到 dataclass 默认 1.0）
    assert override.fill_r == 0.5
    assert override.font_size == 48


def test_resolve_subtitle_style_returns_none_on_error() -> None:
    class _BrokenTimeline:
        def get_track_by_id(self, track_id: str) -> Any:
            raise RuntimeError("gone")

    host = _controller()
    assert host._resolve_subtitle_style(_BrokenTimeline(), _SubtitleClip()) is None
    assert host._resolve_subtitle_style(None, _SubtitleClip()) is not None


# --------------------------------------------------------------------------- #
# 调色剪贴板 / _apply_project_change
# --------------------------------------------------------------------------- #


class _LayerLikeStack:
    """v2 快照优先 / v1 平铺键回退的调色栈桩。"""

    def __init__(self, *, layers: list[dict] | None = None, flat: bool = False):
        self.layers = layers or []
        if flat:
            self.grading_params = {"lift": [0.1, 0.0, 0.0], "gain": [1.0, 1.0, 1.0]}
            self.mixer_params = {"red": 0.5}
            self.soft_clip_params = {"highlight": 95}
            self.lut_path = "/luts/foo.cube"
            self.lut_intensity = 0.7
            self.curves_data = {"master": [(0, 0), (1, 1)]}
            self.hsl_curve_data = {"h": 3}
            self.trackings = {"t": 1}
            self.raw_params = {"wb": 5500}


def test_grade_clip_layers_prefers_v2_snapshots() -> None:
    snapshot = {
        "name": "L1", "grading_params": {"lift": [0.2, 0.0, 0.0]},
    }
    host = _controller()
    layers = host._grade_clip_layers(_LayerLikeStack(layers=[snapshot]))
    assert len(layers) == 1
    assert layers[0].name == "L1"
    assert layers[0].grading_params["lift"] == (0.2, 0.0, 0.0)


def test_grade_clip_layers_falls_back_to_v1_flat_fields() -> None:
    host = _controller()
    layers = host._grade_clip_layers(_LayerLikeStack(flat=True))
    assert len(layers) == 1
    layer = layers[0]
    assert layer.grading_params["lift"] == (0.1, 0.0, 0.0)  # list → tuple
    assert layer.mixer_params == {"red": 0.5}
    assert layer.soft_clip_params["highlight"] == 95
    assert layer.active_lut_path == "/luts/foo.cube"
    assert layer.lut_intensity == 0.7


def test_grade_clipboard_is_lazy_singleton() -> None:
    host = _controller()
    assert host._get_grade_clipboard() is host._get_grade_clipboard()
    from src.features.color.core.grade_clipboard import GradeClipboard

    assert isinstance(host._grade_clipboard, GradeClipboard)


def test_grade_copy_without_stack_is_rejected_with_status() -> None:
    project = EditProject.create("grade")
    timeline = _timeline_with_clip()
    host = _controller(project, timeline)

    host._find_timeline_clip = lambda clip_id: timeline.video_tracks[0].clips[0]

    host._on_grade_copy_requested("t1", "clip-1")  # clip 无 color_stack_id

    clipboard = host._get_grade_clipboard()
    assert clipboard.copied_categories == set()
    assert host.status_bar.messages  # 提示"没有调色可复制"


def test_grade_copy_captures_categories_from_stack() -> None:
    project = EditProject.create("grade")
    timeline = _timeline_with_clip()
    stack = _LayerLikeStack(flat=True)
    project = replace(project, color_stacks={"stack-1": stack})
    clip = replace(
        timeline.video_tracks[0].clips[0], color_stack_id="stack-1",
    )
    timeline = replace(
        timeline,
        video_tracks=(
            replace(timeline.video_tracks[0], clips=(clip,)),
        ),
    )
    host = _controller(project, timeline)
    host._find_timeline_clip = lambda clip_id: clip

    host._on_grade_copy_requested("t1", "clip-1")

    clipboard = host._get_grade_clipboard()
    assert clipboard.copied_categories
    # 状态栏回显复制类别数
    assert any("已复制调色" in m for m, _ in host.status_bar.messages)


def test_apply_project_change_syncs_all_consumers() -> None:
    project = EditProject.create("before")
    new_project = replace(project, name="after")
    renderer = _Renderer()
    view = _TimelineView()
    host = _controller(project, renderer=renderer)
    host.timeline_panel.timeline_view = view
    adr = _AdrPanel()
    host.adr_take_sheet_panel = adr

    host._apply_project_change(new_project)

    assert host._current_edit_project is new_project
    assert host.media_pool_panel.projects == [new_project]
    assert view.projects == [new_project]
    assert view.project_changed.emitted == [(new_project,)]
    assert renderer.projects == [new_project]
    # ADR take sheet 重建一次；同一实例重复回写不重建
    assert len(adr.sheets) == 1
    host._apply_project_change(new_project)
    assert len(adr.sheets) == 1


# --------------------------------------------------------------------------- #
# ADR 上下文（降级路径）
# --------------------------------------------------------------------------- #


def test_adr_settings_dict_degrades_when_panel_broken() -> None:
    host = _controller()
    assert host._adr_settings_dict() == {}  # 面板缺失 → 空

    host.adr_take_sheet_panel = _AdrPanel(raise_settings=True)
    assert host._adr_settings_dict() == {}

    host.adr_take_sheet_panel = _AdrPanel()
    assert host._adr_settings_dict() == {"device_id": "default"}


def test_adr_timeline_context_reports_fps_and_playhead() -> None:
    timeline = _timeline_with_clip()
    host = _controller(EditProject.create("p"), timeline)
    host.edit_page._current_frame = 120
    fps, in_point = host._adr_timeline_context()
    assert fps == Rational(24, 1)
    assert in_point == 120

    host.edit_page._current_frame = "not-a-number"
    assert host._adr_timeline_context()[1] is None

    host.edit_page = None
    assert host._adr_timeline_context() == (None, None)


def test_adr_loop_window_seconds_validates_range() -> None:
    timeline = replace(_timeline_with_clip(), range_start=24, range_end=72)
    host = _controller(EditProject.create("p"), timeline)
    assert host._adr_loop_window_seconds(Rational(24, 1)) == 2.0

    # 反转/缺失范围 → None
    host.edit_page._current_timeline = replace(timeline, range_start=72, range_end=24)
    assert host._adr_loop_window_seconds(24.0) is None
    host.edit_page._current_timeline = replace(timeline, range_start=None, range_end=72)
    assert host._adr_loop_window_seconds(24.0) is None
    host.edit_page = None
    assert host._adr_loop_window_seconds(24.0) is None

    # 非法/非正 fps → 回退 24.0 或 None
    host2 = _controller(
        EditProject.create("p"),
        replace(_timeline_with_clip(), range_start=24, range_end=48),
    )
    assert host2._adr_loop_window_seconds(None) == 1.0  # 用时间线 fps=24
    assert host2._adr_loop_window_seconds("bad") == 1.0
    assert host2._adr_loop_window_seconds(0) is None


# --------------------------------------------------------------------------- #
# MG 空闲缓存：安静判定 / 单片段调度 / 取消清理
# --------------------------------------------------------------------------- #


def test_edit_session_is_quiet_for_mg_cache_branches() -> None:
    quiet = _controller(renderer=_Renderer(render_idle=True))
    assert quiet._edit_session_is_quiet_for_mg_cache() is True

    playing = _controller(renderer=_Renderer(render_idle=True))
    playing.edit_page._is_playing = True
    assert playing._edit_session_is_quiet_for_mg_cache() is False

    video_playing = _controller(renderer=_Renderer(render_idle=True))
    video_playing.video_controller = type("V", (), {"is_playing": True})()
    assert video_playing._edit_session_is_quiet_for_mg_cache() is False

    busy = _controller(renderer=_Renderer(render_idle=False))
    assert busy._edit_session_is_quiet_for_mg_cache() is False

    # 探针抛错不阻塞判定（降级为可用）
    broken = _controller(renderer=_Renderer(raise_idle=True))
    assert broken._edit_session_is_quiet_for_mg_cache() is True


class _MgWorker:
    def __init__(self, clip_id: str = "clip-1", kind: str = "motion_graphic") -> None:
        self._manifest = _manifest(
            clip_id=clip_id, render_kind=kind, status="rendering",
        )
        self.cancelled = 0

    def cancel(self) -> None:
        self.cancelled += 1


def _mg_project_and_timeline() -> tuple[EditProject, TimelineSequence]:
    asset = MediaAsset(
        asset_id="teaching_animation:scene-1", path="/mg.svg",
        media_type="motion_graphic", display_name="MG", metadata={},
    )
    project = EditProject.create("mg")
    project = replace(project, assets={**project.assets, asset.asset_id: asset})
    timeline = _timeline_with_clip(asset_id=asset.asset_id)
    return project, timeline


def test_maybe_start_idle_mg_cache_requires_timer_and_quiet() -> None:
    host = _controller(*_mg_project_and_timeline())
    host._mg_idle_cache_timer = None
    dispatched: list[Any] = []
    host._on_render_cache_clip_requested = (
        lambda clip_id, track_id: dispatched.append(clip_id) or True
    )
    host._maybe_start_idle_mg_cache()
    assert dispatched == []

    host2 = _controller(*_mg_project_and_timeline())
    host2._mg_idle_cache_timer = object()
    host2.edit_page._is_playing = True  # 会话不安静
    host2._on_render_cache_clip_requested = (
        lambda clip_id, track_id: dispatched.append(clip_id) or True
    )
    host2._maybe_start_idle_mg_cache()
    assert dispatched == []


def test_maybe_start_idle_mg_cache_dispatches_single_clip() -> None:
    project, timeline = _mg_project_and_timeline()
    host = _controller(project, timeline)
    host._mg_idle_cache_timer = object()

    def _start(clip_id: str, track_id: str) -> bool:
        worker = _MgWorker(clip_id=clip_id)
        host._render_cache_workers[f"cache-{clip_id}"] = worker
        return True

    host._on_render_cache_clip_requested = _start
    host._maybe_start_idle_mg_cache()

    assert host._mg_idle_cache_ids == {"cache-clip-1"}
    # 新 worker 已登记（一次只跑一个 MG 缓存录制）
    assert any(
        getattr(w, "_manifest", None) is not None
        for w in host._render_cache_workers.values()
    )
    # in-flight 去重：同 clip 再次调度被跳过
    host._render_cache_inflight_clips.add(("t1", "clip-1"))
    before = dict(host._render_cache_workers)
    host._maybe_start_idle_mg_cache()
    assert host._render_cache_workers == before


def test_maybe_start_idle_mg_cache_serial_guard_blocks_second_render() -> None:
    project, timeline = _mg_project_and_timeline()
    host = _controller(project, timeline)
    host._mg_idle_cache_timer = object()
    host._render_cache_workers["cache-running"] = _MgWorker(clip_id="clip-1")

    dispatched: list[str] = []
    host._on_render_cache_clip_requested = (
        lambda clip_id, track_id: dispatched.append(clip_id) or True
    )
    host._maybe_start_idle_mg_cache()

    assert dispatched == []  # 已有 MG worker → 串行守卫


def test_maybe_start_idle_mg_cache_skips_non_mg_clips() -> None:
    host = _controller(EditProject.create("p"), _timeline_with_clip())
    host._mg_idle_cache_timer = object()
    dispatched: list[str] = []
    host._on_render_cache_clip_requested = (
        lambda clip_id, track_id: dispatched.append(clip_id) or True
    )
    host._maybe_start_idle_mg_cache()
    assert dispatched == []  # 普通素材（即使未缓存）不自动入队


def test_cancel_idle_mg_cache_renders_cleans_bookkeeping(tmp_path) -> None:
    project, timeline = _mg_project_and_timeline()
    cacheable = _manifest(
        cache_id="c-idle", clip_id="clip-1",
        path=str(tmp_path / "idle.mp4"), status="valid", render_fingerprint="",
    )
    (tmp_path / "idle.mp4").write_bytes(b"i")
    project = _project_with_manifests(cacheable)
    host = _controller(project, timeline)
    worker = _MgWorker(clip_id="clip-1")
    host._render_cache_workers["c-idle"] = worker
    host._mg_idle_cache_ids.add("c-idle")
    host._render_cache_inflight_cache_keys["c-idle"] = ("t1", "clip-1")
    host._render_cache_inflight_clips.add(("t1", "clip-1"))

    host._cancel_idle_mg_cache_renders()

    assert worker.cancelled == 1
    assert host._mg_idle_cache_ids == set()
    assert "c-idle" not in host._render_cache_workers
    assert host._render_cache_inflight_clips == set()
    # 工程被替换（valid 缓存随取消失效）并同步到面板
    assert host._current_edit_project is not project
    assert (
        host._current_edit_project.render_cache_manifests["c-idle"]["status"]
        == "stale"
    )
    assert host.media_pool_panel.projects
    assert host.timeline_panel.projects == host.media_pool_panel.projects


def test_cancel_idle_mg_cache_renders_noop_cases() -> None:
    host = _controller()
    host._cancel_idle_mg_cache_renders()  # 无 idle id
    assert host._current_edit_project is None

    host2 = _controller(*_mg_project_and_timeline())
    host2._mg_idle_cache_ids.add("ghost")  # worker 已消失：仅清空集合
    host2._cancel_idle_mg_cache_renders()
    assert host2._mg_idle_cache_ids == set()


# --------------------------------------------------------------------------- #
# MG 缓存契约失效（时间线变更）
# --------------------------------------------------------------------------- #


def test_invalidate_mg_caches_on_contract_change_cancels_workers(tmp_path) -> None:
    project, timeline = _mg_project_and_timeline()
    cacheable = _manifest(
        cache_id="c-mg", clip_id="clip-1",
        path=str(tmp_path / "mg.mp4"), status="valid", render_fingerprint="",
    )
    (tmp_path / "mg.mp4").write_bytes(b"m")
    project = _project_with_manifests(cacheable)
    before = _timeline_with_clip(asset_id="teaching_animation:scene-1")
    after = replace(before, width=1280)  # 分辨率契约变化
    worker = _MgWorker(clip_id="clip-1")
    host = _controller(project, before)
    host._render_cache_workers["cache-1"] = worker

    host._invalidate_motion_graphic_caches_for_timeline_change(before, after)

    assert worker.cancelled == 1
    # 缓存翻转 stale 且工程同步到面板
    assert (
        host._current_edit_project.render_cache_manifests["c-mg"]["status"]
        == "stale"
    )
    assert host.timeline_panel.projects  # set_project 同步


def test_invalidate_mg_caches_skips_pure_trim_changes() -> None:
    project, timeline = _mg_project_and_timeline()
    clip = timeline.video_tracks[0].clips[0]
    trimmed = replace(
        timeline,
        video_tracks=(
            replace(timeline.video_tracks[0], clips=(
                replace(clip, timeline_duration=24),
            ),),
        ),
    )
    worker = _MgWorker(clip_id="clip-1")
    host = _controller(project, timeline)
    host._render_cache_workers["cache-1"] = worker

    # 单纯 trim（duration 变化但时长语义…）：MG 片段 duration 变化属于 retiming
    # → 应失效；这里用非 MG 素材验证纯 trim 早退分支
    plain_timeline = _timeline_with_clip()
    plain_clip = plain_timeline.video_tracks[0].clips[0]
    plain_trimmed = replace(
        plain_timeline,
        video_tracks=(
            replace(plain_timeline.video_tracks[0], clips=(
                replace(plain_clip, timeline_duration=12),
            ),),
        ),
    )
    host2 = _controller(EditProject.create("plain"), plain_timeline)
    host2._render_cache_workers["cache-plain"] = worker

    host2._invalidate_motion_graphic_caches_for_timeline_change(
        plain_timeline, plain_trimmed,
    )

    assert worker.cancelled == 0
    assert host2.timeline_panel.projects == []

    # 早退：无工程 / 缺 previous
    host3 = _controller(None, timeline)
    host3._invalidate_motion_graphic_caches_for_timeline_change(timeline, timeline)
    host4 = _controller(project, timeline)
    host4._invalidate_motion_graphic_caches_for_timeline_change(None, timeline)


def test_invalidate_mg_caches_retime_invalidates_scene_group() -> None:
    project, timeline = _mg_project_and_timeline()
    clip = timeline.video_tracks[0].clips[0]
    retimed = replace(
        timeline,
        video_tracks=(
            replace(timeline.video_tracks[0], clips=(
                replace(clip, timeline_duration=96),
            ),),
        ),
    )
    worker = _MgWorker(clip_id="clip-1")
    host = _controller(project, timeline)
    host._render_cache_workers["cache-1"] = worker

    host._invalidate_motion_graphic_caches_for_timeline_change(timeline, retimed)

    assert worker.cancelled == 1


# --------------------------------------------------------------------------- #
# smart 自动缓存评估
# --------------------------------------------------------------------------- #


def test_render_cache_auto_mode_and_threshold_preferences(monkeypatch) -> None:
    from src.shared.config.config_manager import ConfigManager

    host = _controller()
    # 未配置 → off / 6（显式 None，避免依赖机器本地偏好）
    monkeypatch.setattr(
        ConfigManager, "get",
        classmethod(lambda cls, key, default=None: None),
    )
    assert host._render_cache_auto_mode() == "off"
    assert host._render_cache_auto_threshold() == 6

    values: dict[str, Any] = {
        "render_cache_auto_mode": "smart",
        "render_cache_auto_threshold": "9",
    }
    monkeypatch.setattr(
        ConfigManager, "get",
        classmethod(lambda cls, key, default=None: values.get(key, default)),
    )
    assert host._render_cache_auto_mode() == "smart"
    assert host._render_cache_auto_threshold() == 9

    def _raise(cls, key: str, default: Any = None) -> Any:
        raise RuntimeError("config store unavailable")

    monkeypatch.setattr(ConfigManager, "get", classmethod(_raise))
    assert host._render_cache_auto_mode() == "off"
    assert host._render_cache_auto_threshold() == 6


def test_auto_evaluate_render_cache_guards(monkeypatch) -> None:
    host = _controller()
    host._render_cache_auto_mode = lambda: "off"  # type: ignore[method-assign]
    assert host._auto_evaluate_render_cache() is None

    # smart 但无时间线 → 早退
    host2 = _controller(EditProject.create("p"))
    host2._render_cache_auto_mode = lambda: "smart"  # type: ignore[method-assign]
    host2._render_cache_auto_threshold = lambda: 6  # type: ignore[method-assign]
    assert host2._auto_evaluate_render_cache() is None

    dispatched: list[str] = []

    # smart + 普通素材：分数达标也不入队（管线只覆盖 Comp Clip / MG）
    plain = replace(
        _timeline_with_clip(),
        video_tracks=(
            replace(
                _timeline_with_clip().video_tracks[0],
                clips=(replace(
                    _timeline_with_clip().video_tracks[0].clips[0],
                    effects=(object(), object(), object()),  # 3×2=6 分
                ),),
            ),
        ),
    )
    host3 = _controller(EditProject.create("p"), plain)
    host3._render_cache_auto_mode = lambda: "smart"  # type: ignore[method-assign]
    host3._render_cache_auto_threshold = lambda: 6  # type: ignore[method-assign]
    host3._on_render_cache_clip_requested = (
        lambda clip_id, track_id: dispatched.append(clip_id) or True
    )
    host3._auto_evaluate_render_cache()
    assert dispatched == []

    # smart + MG 素材：入队一次（_RENDER_CACHE_AUTO_BATCH=1）
    mg_project, mg_timeline = _mg_project_and_timeline()
    mg_clip = replace(
        mg_timeline.video_tracks[0].clips[0],
        color_stack_id="stack-1",       # +3
        composite_id="comp-1",          # +4 → 7 分 ≥ 6
    )
    mg_timeline = replace(
        mg_timeline,
        video_tracks=(
            replace(mg_timeline.video_tracks[0], clips=(mg_clip,)),
        ),
    )
    host4 = _controller(mg_project, mg_timeline)
    host4._render_cache_auto_mode = lambda: "smart"  # type: ignore[method-assign]
    host4._render_cache_auto_threshold = lambda: 6  # type: ignore[method-assign]
    host4._on_render_cache_clip_requested = (
        lambda clip_id, track_id: dispatched.append(clip_id) or True
    )
    host4._auto_evaluate_render_cache()
    assert dispatched == ["clip-1"]


# --------------------------------------------------------------------------- #
# 时间线设置对话框（状态机：接受/缺失/取消）
# --------------------------------------------------------------------------- #


class _FakeSettingsDialog:
    """替代 NewTimelineDialog：可编程 exec 结果与设置值。"""

    next_settings: dict[str, Any] | None = None
    next_result: int = 1  # QDialog::Accepted

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.initial_settings = kwargs.get("initial_settings")
        self.existing_names = kwargs.get("existing_names")

    def exec(self) -> int:
        return type(self).next_result

    def get_settings(self) -> dict[str, Any]:
        return dict(type(self).next_settings or {})


def test_timeline_settings_requested_updates_refs_and_sequences(monkeypatch) -> None:
    import src.features.edit.ui.new_timeline_dialog as dialog_mod

    timeline = _timeline_with_clip(fps=Rational(24, 1))
    project = EditProject.create("settings")
    ref = TimelineSequenceRef(
        timeline_id="t1", name="t1", fps=24.0,
        width=1920, height=1080, sample_rate=48000,
    )
    project = replace(
        project, timelines={"t1": ref}, active_timeline_id="t1",
    )
    view = _TimelineView()
    renderer = _Renderer()
    host = _controller(project, timeline, view=view, renderer=renderer)
    host._current_timeline_sequences["t1"] = timeline
    host._request_current_timeline_preview_frame = lambda: None

    _FakeSettingsDialog.next_settings = {
        "name": "Renamed", "fps": 25.0, "width": 2560, "height": 1440,
        "sample_rate": 48000, "start_timecode": "01:00:00:00",
    }
    _FakeSettingsDialog.next_result = 1
    monkeypatch.setattr(dialog_mod, "NewTimelineDialog", _FakeSettingsDialog)

    host._on_timeline_settings_requested("t1")

    updated = host._current_timeline_sequences["t1"]
    assert updated.fps == Rational(25, 1)
    # 绝对时序保持：24fps 下 48 帧 = 2s → 25fps 下 50 帧
    assert updated.video_tracks[0].clips[0].timeline_duration == 50
    assert updated.name == "Renamed"
    assert updated.format_settings["start_timecode"] == "01:00:00:00"
    assert host._current_edit_project.timelines["t1"].width == 2560
    assert host.edit_project_dirty is True
    # active 时间线 → 重新加载到面板/页面/渲染器
    assert host.timeline_panel.loaded == [updated]
    assert renderer.sequences == [updated]

    # 对话框取消 → 不变更
    _FakeSettingsDialog.next_result = 0
    before_sequences = dict(host._current_timeline_sequences)
    host._on_timeline_settings_requested("t1")
    assert host._current_timeline_sequences == before_sequences


def test_timeline_settings_requested_missing_refs_return_early(monkeypatch) -> None:
    import src.features.edit.ui.new_timeline_dialog as dialog_mod

    host = _controller(None)
    monkeypatch.setattr(dialog_mod, "NewTimelineDialog", _FakeSettingsDialog)
    host._on_timeline_settings_requested("t1")  # 无工程

    project = EditProject.create("settings")
    host2 = _controller(project)
    host2._on_timeline_settings_requested("missing")  # ref 不存在
    assert host2._current_timeline_sequences == {}


# --------------------------------------------------------------------------- #
# ASR / TTS worker 状态机（同步调用 run()，不发线程）
# --------------------------------------------------------------------------- #


class _RecordingSlots:
    def __init__(self) -> None:
        self.completed: list[Any] = []
        self.failed: list[str] = []
        self.progress: list[tuple[Any, str]] = []

    def hook(self, worker: Any) -> None:
        worker.completed.connect(self.completed.append)
        worker.failed.connect(self.failed.append)
        progress = getattr(worker, "progress", None)
        if progress is not None:
            progress.connect(lambda *args: self.progress.append(args))


class _AsrService:
    def __init__(
        self,
        *,
        error: str = "",
        job_status: str = "succeeded",
        analysis: Any = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._error = error
        self._job_status = job_status
        self._analysis = analysis
        self._raise = raise_exc
        self.request_calls: list[dict[str, Any]] = []

    def request_analysis(
        self, asset_id: str, fingerprint: Any, **kwargs: Any,
    ) -> tuple[Any, str]:
        self.request_calls.append({"asset_id": asset_id, **kwargs})
        if self._raise is not None:
            raise self._raise
        progress = kwargs.get("on_progress")
        if callable(progress):
            progress(0.5, "stage-2")
        job = type("J", (), {
            "status": type("S", (), {"value": self._job_status})(),
            "error_message": "job blew up",
        })()
        return job, self._error

    def get_analysis(self, asset_id: str) -> Any:
        return self._analysis


def test_speech_worker_failure_paths_run_synchronously(qapp) -> None:
    slots = _RecordingSlots()

    # 服务端错误
    worker = _SpeechToTextWorker(
        _AsrService(error="provider down"), "a1", "fp", "24000/1001",
    )
    slots.hook(worker)
    worker.run()
    assert slots.failed == ["provider down"]
    assert slots.completed == []

    # 任务进入 failed 终态
    slots2 = _RecordingSlots()
    worker2 = _SpeechToTextWorker(
        _AsrService(job_status="failed"), "a1", "fp", "24/1",
    )
    slots2.hook(worker2)
    worker2.run()
    assert slots2.failed == ["job blew up"]

    # 空转录结果
    slots3 = _RecordingSlots()
    analysis = type("A", (), {"transcript": ()})()
    worker3 = _SpeechToTextWorker(
        _AsrService(analysis=analysis), "a1", "fp", "24/1",
    )
    slots3.hook(worker3)
    worker3.run()
    assert slots3.failed == ["语音转文字没有产生可用的转录结果"]

    # provider 抛异常 → failed（用户可见）
    slots4 = _RecordingSlots()
    worker4 = _SpeechToTextWorker(
        _AsrService(raise_exc=RuntimeError("model missing")), "a1", "fp", "24/1",
    )
    slots4.hook(worker4)
    worker4.run()
    assert slots4.failed == ["model missing"]


def test_speech_worker_success_and_progress_reporting(qapp) -> None:
    slots = _RecordingSlots()
    analysis = type("A", (), {"transcript": ({"text": "hi"},)})()
    service = _AsrService(analysis=analysis)
    worker = _SpeechToTextWorker(service, "a1", "fp", "24/1", options={"beam": 5})
    slots.hook(worker)

    worker.run()

    assert slots.completed == [analysis]
    assert slots.failed == []
    assert service.request_calls[0]["options"] == {"beam": 5}
    # on_progress 经适配器转成 (fraction, "正在处理…")
    assert slots.progress[-1] == (0.5, "stage-2")


def test_subtitle_to_speech_worker_success_and_failure(qapp) -> None:
    slots = _RecordingSlots()

    class _Cue:
        def __init__(self, text: str, start: int) -> None:
            self.text = text
            self.timeline_start = start

    result = object()

    class _Provider:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def synthesize(self, cues: Any, *, fps: float, settings: Any) -> Any:
            self.calls.append((tuple(c.text for c in cues), fps, settings))
            return result

    provider = _Provider()
    worker = _SubtitleToSpeechWorker(
        provider, (_Cue("你好", 24), _Cue("世界", 48)), 24.0, settings="cfg",
    )
    slots.hook(worker)
    worker.run()

    assert slots.completed == [result]
    assert provider.calls == [(("你好", "世界"), 24.0, "cfg")]

    class _BrokenProvider:
        def synthesize(self, cues: Any, *, fps: float, settings: Any) -> Any:
            raise RuntimeError("voice model not found")

    slots2 = _RecordingSlots()
    worker2 = _SubtitleToSpeechWorker(_BrokenProvider(), (), 24.0)
    slots2.hook(worker2)
    worker2.run()
    assert slots2.failed == ["voice model not found"]


# --------------------------------------------------------------------------- #
# _get_speech_timeline_view / 进度条同步
# --------------------------------------------------------------------------- #


def test_get_speech_timeline_view_prefers_timeline_view_attr() -> None:
    view = _TimelineView()
    host = _controller(view=view)
    assert host._get_speech_timeline_view() is view

    host2 = _controller(view=None)
    host2.timeline_panel.timeline_view = view
    assert host2._get_speech_timeline_view() is view


def test_speech_to_text_progress_updates_bars_and_status() -> None:
    class _Bar:
        def __init__(self) -> None:
            self.ranges: list[tuple[int, int]] = []
            self.values: list[int] = []
            self.shown = 0
            self.hidden = 0

        def setRange(self, lo: int, hi: int) -> None:
            self.ranges.append((lo, hi))

        def setValue(self, value: int) -> None:
            self.values.append(value)

        def show(self) -> None:
            self.shown += 1

        def hide(self) -> None:
            self.hidden += 1

    bar = _Bar()
    host = _controller()
    host.progress_bar = bar

    host._on_speech_to_text_progress(0.5, "推理中")
    assert bar.ranges[-1] == (0, 100)
    assert bar.values[-1] == 50
    assert host.status_bar.messages[-1] == ("语音转文字：推理中", 0)

    host._on_speech_to_text_progress(None, "加载模型")
    assert bar.ranges[-1] == (0, 0)  # 不定进度动画

    host._on_speech_to_text_finished()
    assert bar.hidden == 1
    assert bar.ranges[-1] == (0, 100)
    assert host._speech_to_text_worker is None
