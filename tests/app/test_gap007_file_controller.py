"""GAP-007 补测：file_controller.py 高风险路径（批 D 文件侧最后缺口）。

针对 GAP_REPORT Top-30 #7（src/app/controllers/file_controller.py，22% 覆盖、
缺失约 3070 行）补测以下高风险路径，不凑行数：

* 工程打开路由（_open_project_path / _read_project_mode_from_manifest /
  _check_project_matches_work_mode / _load_standalone_project /
  _load_edit_project 的错误与恢复分支）
* 关闭前保存确认（_has_unsaved_changes / _confirm_save_before_close 的
  Save/Discard/Cancel/保存失败矩阵）
* 关闭时持久化（_persist_active_project_before_close 的 Discard 跳过与
  异常吞没）
* 菜单/最近文件接线（_add_to_recent_files / _open_recent_file /
  dropEvent / dragEnterEvent / _open_media_path 分发）
* new_project / close_project 工作模式路由
* 元数据异步加载器（缓存命中 / in-flight 去重 / worker 异常回退）
* GPU 渲染线程停机推迟销毁（_stop_gpu_renderer_deferred）

不可测路径（真机 GPU / 真实 GL 上下文 / exiftool 子进程网络）均以替身隔离。
"""

from __future__ import annotations

import os
import zipfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest

from src.app.controllers import file_controller as fc
from src.app.controllers.file_controller import FileOpsMixin

pytestmark = pytest.mark.app


# ═══════════════════════════════════════════════════════════════════════
# 测试基座：QMessageBox 记录替身 / host 工厂 / .topos 工程包工厂
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def msgbox(monkeypatch) -> SimpleNamespace:
    """把 file_controller 模块内的 QMessageBox 换成记录用替身（不弹窗）。

    替身继承真实 QMessageBox，保留 StandardButton 枚举供被测代码比较；
    question 的返回值可经 ``state["question_result"]`` 配置。
    """
    from PyQt6.QtWidgets import QMessageBox

    calls: list[tuple[str, tuple[Any, ...]]] = []
    state: dict[str, Any] = {"question_result": None}

    class _Recording(QMessageBox):
        @staticmethod
        def _rec(name: str, args: tuple[Any, ...]) -> None:
            calls.append((name, args))

        @staticmethod
        def critical(*args: Any, **kwargs: Any) -> None:
            _Recording._rec("critical", args)

        @staticmethod
        def question(*args: Any, **kwargs: Any) -> Any:
            _Recording._rec("question", args)
            return state["question_result"]

        @staticmethod
        def information(*args: Any, **kwargs: Any) -> None:
            _Recording._rec("information", args)

        @staticmethod
        def warning(*args: Any, **kwargs: Any) -> None:
            _Recording._rec("warning", args)

    monkeypatch.setattr(fc, "QMessageBox", _Recording)
    return SimpleNamespace(calls=calls, state=state)


def _make_topos(path: str, project_mode: str | None) -> str:
    """构造带 manifest.json 的真实 .topos zip（project_mode=None 时省略键）。"""
    manifest: dict[str, Any] = {}
    if project_mode is not None:
        manifest["project_mode"] = project_mode
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", _json_dumps(manifest))
    return str(path)


def _json_dumps(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data)


class _StubConfig:
    """不落盘的 ConfigManager 替身。"""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(data or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self._data[key] = value

    def save_config(self) -> None:
        self.saved = True  # type: ignore[attr-defined]


def make_host(**overrides: Any) -> SimpleNamespace:
    """FileOpsMixin 逻辑替身宿主：仅挂被测方法触到的属性。"""
    host = SimpleNamespace(
        _current_edit_project=None,
        _current_edit_project_filepath=None,
        _current_timeline_sequences={},
        current_file_path=None,
        current_file_is_video=False,
        current_page="Edit",
        edit_project_dirty=False,
        edit_project_io=None,
        config_manager=_StubConfig(),
        save_router=MagicMock(name="save_router"),
        status_bar=SimpleNamespace(showMessage=Mock(name="showMessage")),
        media_pool_panel=SimpleNamespace(set_project=Mock(name="mp.set_project")),
        timeline_panel=SimpleNamespace(
            set_project=Mock(name="tp.set_project"),
            clear=Mock(name="tp.clear"),
            show_provisional_timeline=Mock(name="tp.provisional"),
            set_timeline_sequence=Mock(name="tp.set_seq"),
        ),
        engine=None,
        titles=[],
        recent_projects=[],
        mg_roots=[],
        close_results=[True],
        load_image_calls=[],
        load_video_calls=[],
        open_project_calls=[],
        unload_calls=[],
        create_video_project_called=[False],
        adjustments_reset_called=[False],
    )
    host.setWindowTitle = lambda title: host.titles.append(title)
    host._add_to_recent_projects = lambda p: host.recent_projects.append(p)
    host._sync_mg_preset_project_root = lambda p: host.mg_roots.append(p)
    host.close_project = lambda: host.close_results.pop(0)
    host.load_image = lambda path, **kw: host.load_image_calls.append((path, kw))
    host.load_video = lambda path, **kw: host.load_video_calls.append((path, kw))
    host._open_project_path = lambda p: host.open_project_calls.append(p)
    host._create_video_project = lambda: host.create_video_project_called.__setitem__(0, True)
    host.on_adjustments_reset = lambda: host.adjustments_reset_called.__setitem__(0, True)
    host.unload_video = lambda **kw: host.unload_calls.append(kw)
    host._get_work_mode = lambda: "all"
    host._check_project_matches_work_mode = lambda filepath: True
    # 混入方法互调委托回真实实现（host 不是 FileOpsMixin 实例）
    host._clear_all_dirty_states = lambda: FileOpsMixin._clear_all_dirty_states(host)
    host._has_unsaved_changes = lambda: FileOpsMixin._has_unsaved_changes(host)
    host._report_content_migrations = lambda: FileOpsMixin._report_content_migrations(host)
    host._report_package_compatibility = lambda: FileOpsMixin._report_package_compatibility(host)
    host._restore_edit_ui_state = lambda state: FileOpsMixin._restore_edit_ui_state(host, state)
    # 默认委托真实实现：_open_project_path / _open_media_path 的路由测试
    # 依赖真实 manifest.zip 读取。
    host._read_project_mode_from_manifest = (
        lambda filepath: FileOpsMixin._read_project_mode_from_manifest(host, filepath)
    )

    for key, value in overrides.items():
        setattr(host, key, value)
    return host


# ═══════════════════════════════════════════════════════════════════════
# manifest 轻量读取（真实 zip）
# ═══════════════════════════════════════════════════════════════════════


def test_read_project_mode_from_real_manifest(tmp_path) -> None:
    path = _make_topos(str(tmp_path / "a.topos"), "edit_project")
    host = make_host()
    assert FileOpsMixin._read_project_mode_from_manifest(host, path) == "edit_project"


def test_read_project_mode_maps_legacy_single_asset(tmp_path) -> None:
    """旧名 single_asset 必须映射为 standalone_image（与 Manifest 一致）。"""
    path = _make_topos(str(tmp_path / "legacy.topos"), "single_asset")
    host = make_host()
    assert FileOpsMixin._read_project_mode_from_manifest(host, path) == "standalone_image"


def test_read_project_mode_defaults_when_key_missing(tmp_path) -> None:
    path = _make_topos(str(tmp_path / "bare.topos"), None)
    host = make_host()
    assert FileOpsMixin._read_project_mode_from_manifest(host, path) == "standalone_image"


def test_read_project_mode_returns_none_for_corrupt_archive(tmp_path) -> None:
    bad = tmp_path / "broken.topos"
    bad.write_bytes(b"this is not a zip archive")
    host = make_host()
    assert FileOpsMixin._read_project_mode_from_manifest(host, str(bad)) is None


def test_read_project_mode_returns_none_when_manifest_missing(tmp_path) -> None:
    empty = tmp_path / "empty.topos"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("other.json", "{}")
    host = make_host()
    assert FileOpsMixin._read_project_mode_from_manifest(host, str(empty)) is None


# ═══════════════════════════════════════════════════════════════════════
# 工作模式闸门 _check_project_matches_work_mode
# ═══════════════════════════════════════════════════════════════════════


def test_work_mode_check_passes_when_mode_matches(tmp_path, msgbox) -> None:
    path = _make_topos(str(tmp_path / "img.topos"), "standalone_image")
    host = make_host()
    host._get_work_mode = lambda: "image"

    assert FileOpsMixin._check_project_matches_work_mode(host, path) is True
    assert msgbox.calls == []  # 匹配时不得弹窗


def test_work_mode_check_mismatch_blocks_load_with_dialog(tmp_path, msgbox) -> None:
    """video 工程在 image 模式下必须被拒并提示切换模式（而非静默失败）。"""
    path = _make_topos(str(tmp_path / "tl.topos"), "edit_project")
    host = make_host()
    host._get_work_mode = lambda: "image"

    assert FileOpsMixin._check_project_matches_work_mode(host, path) is False
    assert [name for name, _ in msgbox.calls] == ["information"]


def test_work_mode_check_unreadable_manifest_defers_to_load(tmp_path, msgbox) -> None:
    """manifest 读不出来时放行走正常加载（由加载路径报错）。"""
    bad = tmp_path / "bad.topos"
    bad.write_bytes(b"garbage")
    host = make_host()
    host._get_work_mode = lambda: "image"

    assert FileOpsMixin._check_project_matches_work_mode(host, str(bad)) is True
    assert msgbox.calls == []


def test_get_work_mode_defaults_to_all_on_flag_error(monkeypatch) -> None:
    import src.shared.feature_flags as flags

    def _boom(_cfg: object) -> str:
        raise RuntimeError("flag store unavailable")

    monkeypatch.setattr(flags, "get_work_mode", _boom)
    host = make_host()
    assert FileOpsMixin._get_work_mode(host) == "all"


# ═══════════════════════════════════════════════════════════════════════
# 工程打开路由 _open_project_path
# ═══════════════════════════════════════════════════════════════════════


def test_open_project_path_rejects_missing_file() -> None:
    host = make_host()
    loaded: list[str] = []
    host._load_edit_project = lambda p: loaded.append(p)

    assert FileOpsMixin._open_project_path(host, "/nonexistent/x.topos") is False
    assert loaded == []


def test_open_project_path_aborts_when_closing_active_project_cancelled(
        tmp_path) -> None:
    """已有工程时打开新工程：close_project 被取消必须中止加载。"""
    path = _make_topos(str(tmp_path / "n.topos"), "edit_project")
    host = make_host(_current_edit_project=object())
    host.close_results = [False]
    host._load_edit_project = Mock(name="_load_edit_project")

    assert FileOpsMixin._open_project_path(host, path) is False
    host._load_edit_project.assert_not_called()


def test_open_project_path_routes_standalone_manifest_to_image_loader(
        tmp_path) -> None:
    path = _make_topos(str(tmp_path / "s.topos"), "standalone_image")
    host = make_host(current_page="Edit")
    host._load_standalone_project = Mock(name="_load_standalone_project")
    host._load_edit_project = Mock(name="_load_edit_project")
    host._load_standalone_project.side_effect = (
        lambda p: setattr(host, "_current_edit_project", object())
    )

    assert FileOpsMixin._open_project_path(host, path) is True
    host._load_standalone_project.assert_called_once_with(path)
    host._load_edit_project.assert_not_called()
    # standalone 工程不强制切到 Edit 页（保持当前页）
    assert host.current_page == "Edit"


def test_open_project_path_switches_to_edit_page_for_timeline_project(
        tmp_path) -> None:
    path = _make_topos(str(tmp_path / "e.topos"), "edit_project")
    host = make_host(current_page="Color")
    switched: list[str] = []
    host.switch_to_page = lambda name: switched.append(name)
    host._load_edit_project = lambda p: setattr(host, "_current_edit_project", object())

    assert FileOpsMixin._open_project_path(host, path) is True
    assert switched == ["Edit"]
    # 成功加载后 MG preset 工程根指向新工程文件
    assert host.mg_roots == [path]


def test_open_project_path_reports_not_loaded_when_loader_keeps_project(
        tmp_path) -> None:
    """加载失败（工程引用未变化）必须如实返回 False。"""
    path = _make_topos(str(tmp_path / "f.topos"), "edit_project")
    existing = object()
    host = make_host(_current_edit_project=existing)
    host._load_edit_project = Mock(name="_load_edit_project")  # 不替换工程

    assert FileOpsMixin._open_project_path(host, path) is False
    assert host.mg_roots == []


# ═══════════════════════════════════════════════════════════════════════
# _load_standalone_project 错误路径与水合
# ═══════════════════════════════════════════════════════════════════════


def _standalone_project(name: str = "shoot", with_timelines: bool = False) -> SimpleNamespace:
    asset = SimpleNamespace(media_type="image", path="/tmp/img.png")
    return SimpleNamespace(
        assets={"a1": asset},
        timelines=[object()] if with_timelines else [],
        name=name,
    )


def test_load_standalone_project_reports_io_exception(tmp_path, msgbox) -> None:
    host = make_host()
    io = MagicMock(name="edit_project_io")
    io.load_project.side_effect = OSError("archive corrupted")
    io.last_content_migrations = ()
    host.edit_project_io = io

    FileOpsMixin._load_standalone_project(host, str(tmp_path / "x.topos"))

    assert [name for name, _ in msgbox.calls] == ["critical"]
    assert host._current_edit_project is None  # 状态不被半途污染


def test_load_standalone_project_reports_none_result(tmp_path, msgbox) -> None:
    host = make_host()
    io = MagicMock(name="edit_project_io")
    io.load_project.return_value = None
    io.last_content_migrations = ()
    host.edit_project_io = io

    FileOpsMixin._load_standalone_project(host, str(tmp_path / "x.topos"))

    assert [name for name, _ in msgbox.calls] == ["critical"]


def test_load_standalone_project_rejects_timeline_project(tmp_path, msgbox) -> None:
    """manifest 声称 standalone 但内容含时间线 → 拒绝（不得清掉现有状态）。"""
    host = make_host(_current_edit_project=object())
    io = MagicMock(name="edit_project_io")
    sequences = {"t": object()}
    io.load_project.return_value = (_standalone_project(with_timelines=True), None, sequences)
    io.last_content_migrations = ()
    host.edit_project_io = io
    before = host._current_edit_project

    FileOpsMixin._load_standalone_project(host, str(tmp_path / "x.topos"))

    assert [name for name, _ in msgbox.calls] == ["critical"]
    assert host._current_edit_project is before
    assert host.load_image_calls == []


def test_load_standalone_project_rejects_project_without_image_asset(
        tmp_path, msgbox) -> None:
    host = make_host()
    project = SimpleNamespace(
        assets={"v1": SimpleNamespace(media_type="video", path="/tmp/v.mp4")},
        timelines=[],
        name="only-video",
    )
    io = MagicMock(name="edit_project_io")
    io.load_project.return_value = (project, None, {})
    io.last_content_migrations = ()
    host.edit_project_io = io

    FileOpsMixin._load_standalone_project(host, str(tmp_path / "x.topos"))

    assert [name for name, _ in msgbox.calls] == ["critical"]


def test_load_standalone_project_hydrates_state_and_reopens_image(tmp_path) -> None:
    """成功路径：状态水合、脏标记清零、按工程路径重开图像、记账最近工程。"""
    path = str(tmp_path / "graded.topos")
    project = _standalone_project()
    sequences = {"t": object()}
    io = MagicMock(name="edit_project_io")
    io.load_project.return_value = (project, None, sequences)
    io.last_content_migrations = ()
    host = make_host(edit_project_dirty=True)
    host.edit_project_io = io

    FileOpsMixin._load_standalone_project(host, path)

    assert host._current_edit_project is project
    assert host._current_edit_project_filepath == path
    assert host._current_timeline_sequences is sequences
    host.save_router.set_project_path.assert_called_once_with(path)
    assert host.edit_project_dirty is False  # _clear_all_dirty_states
    host.media_pool_panel.set_project.assert_called_once_with(project)
    host.timeline_panel.set_project.assert_called_once_with(project)
    assert host.load_image_calls == [("/tmp/img.png", {"project_path": path})]
    assert host.recent_projects == [path]
    assert host.config_manager.get("last_project_path") == os.path.dirname(path)
    assert host.titles == [f"Topos - {project.name}"]


def test_report_content_migrations_surfaces_schema_entries(msgbox) -> None:
    """P2-13：迁移条目要同时上状态栏与日志（空条目则完全静默）。"""
    io = SimpleNamespace(last_content_migrations=("v1→v2: layers",))
    host = make_host(edit_project_io=io)

    FileOpsMixin._report_content_migrations(host)
    host.status_bar.showMessage.assert_called_once()

    quiet = make_host(edit_project_io=SimpleNamespace(last_content_migrations=()))
    FileOpsMixin._report_content_migrations(quiet)
    quiet.status_bar.showMessage.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# _load_edit_project：恢复（autosave）分支与失败路径
# ═══════════════════════════════════════════════════════════════════════


def _edit_io_for(project: SimpleNamespace, ui_state: Any = None,
                 sequences: dict[str, Any] | None = None) -> MagicMock:
    io = MagicMock(name="edit_project_io")
    io.load_project.return_value = (project, ui_state, sequences or {})
    io.get_autosave_filepath.return_value = None
    io.last_content_migrations = ()
    return io


def _edit_project(name: str = "T", active_timeline_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, active_timeline_id=active_timeline_id)


def test_load_edit_project_bails_without_edit_io(monkeypatch) -> None:
    """Image 模式（edit 禁用）下防御性直接返回，不触发任何加载。"""
    host = make_host(edit_project_io=None)
    host._check_project_matches_work_mode = lambda p: True

    FileOpsMixin._load_edit_project(host, "/tmp/any.topos")

    assert host._current_edit_project is None


def test_load_edit_project_aborts_on_work_mode_mismatch(tmp_path) -> None:
    path = _make_topos(str(tmp_path / "m.topos"), "edit_project")
    host = make_host()
    host._check_project_matches_work_mode = lambda p: False
    host.edit_project_io = _edit_io_for(_edit_project())

    FileOpsMixin._load_edit_project(host, path)

    host.edit_project_io.load_project.assert_not_called()


def test_load_edit_project_restores_from_autosave_when_confirmed(
        tmp_path, msgbox) -> None:
    """选 Yes：从恢复文件加载，但活跃路径保持原工程文件。"""
    from PyQt6.QtWidgets import QMessageBox

    path = str(tmp_path / "proj.topos")
    autosave = str(tmp_path / "proj.topos.autosave")
    io = _edit_io_for(_edit_project())
    io.get_autosave_filepath.return_value = autosave
    host = make_host(edit_project_io=io)
    host._restore_edit_ui_state = Mock(name="_restore_edit_ui_state")
    host._init_timeline_preview_renderer = Mock()
    host._setup_ai_editing = Mock()
    msgbox.state["question_result"] = QMessageBox.StandardButton.Yes

    FileOpsMixin._load_edit_project(host, path)

    io.load_project.assert_called_once_with(autosave)
    io.set_current_filepath.assert_called_once_with(path)
    assert host._current_edit_project_filepath == path
    host.save_router.set_project_path.assert_called_once_with(path)


def test_load_edit_project_discards_autosave_when_declined(tmp_path, msgbox) -> None:
    """选 No：丢弃恢复快照（否则每次启动都再问一遍），按原文件加载。"""
    from PyQt6.QtWidgets import QMessageBox

    path = str(tmp_path / "proj.topos")
    io = _edit_io_for(_edit_project())
    io.get_autosave_filepath.return_value = str(tmp_path / "proj.autosave")
    host = make_host(edit_project_io=io)
    host._restore_edit_ui_state = Mock(name="_restore_edit_ui_state")
    host._init_timeline_preview_renderer = Mock()
    host._setup_ai_editing = Mock()
    msgbox.state["question_result"] = QMessageBox.StandardButton.No

    FileOpsMixin._load_edit_project(host, path)

    io.discard_autosave.assert_called_once_with(path)
    io.load_project.assert_called_once_with(path)  # 未从恢复文件加载


def test_load_edit_project_reports_failure_when_result_none(tmp_path, msgbox) -> None:
    path = str(tmp_path / "bad.topos")
    io = _edit_io_for(_edit_project())
    io.load_project.return_value = None
    host = make_host(edit_project_io=io)

    FileOpsMixin._load_edit_project(host, path)

    assert [name for name, _ in msgbox.calls] == ["critical"]
    assert host._current_edit_project is None


def test_load_edit_project_restores_active_sequence_and_ui_state(tmp_path) -> None:
    """恢复路径：活跃时间线序列回填 TimelinePanel + UI 状态（playhead/zoom）。"""
    path = str(tmp_path / "full.topos")
    sequence = SimpleNamespace(name="V1")
    project = _edit_project(active_timeline_id="t1")
    ui_state = SimpleNamespace(playhead_frame=42, zoom_level=2.5, expanded_bins=[],
                               view_mode="list", timeline_layout=None)
    io = _edit_io_for(project, ui_state=ui_state, sequences={"t1": sequence})
    timeline_panel = SimpleNamespace(
        set_project=Mock(name="tp.set_project"),
        set_playhead_frame=Mock(name="set_playhead"),
        set_zoom_level=Mock(name="set_zoom"),
        clear=Mock(name="clear"),
        show_provisional_timeline=Mock(name="provisional"),
        set_timeline_sequence=Mock(name="set_seq"),
    )
    host = make_host(edit_project_io=io, timeline_panel=timeline_panel)
    host._init_timeline_preview_renderer = Mock()
    host._setup_ai_editing = Mock()

    FileOpsMixin._load_edit_project(host, path)

    timeline_panel.set_timeline_sequence.assert_called_once_with(sequence)
    timeline_panel.set_playhead_frame.assert_called_once_with(42)
    timeline_panel.set_zoom_level.assert_called_once_with(2.5)
    timeline_panel.show_provisional_timeline.assert_not_called()
    host._init_timeline_preview_renderer.assert_called_once()
    host._setup_ai_editing.assert_called_once()
    assert host.recent_projects == [path]
    assert host.titles == [f"Topos - {project.name}"]


def test_load_edit_project_falls_back_to_provisional_timeline(tmp_path) -> None:
    """首次未保存媒体的工程没有持久化序列 → 面板给临时 V1/A1 布局。"""
    path = str(tmp_path / "fresh.topos")
    io = _edit_io_for(_edit_project(active_timeline_id="missing"), sequences={})
    timeline_panel = SimpleNamespace(
        set_project=Mock(name="tp.set_project"),
        clear=Mock(name="clear"),
        show_provisional_timeline=Mock(name="provisional"),
        set_timeline_sequence=Mock(name="set_seq"),
    )
    host = make_host(edit_project_io=io, timeline_panel=timeline_panel)
    host._init_timeline_preview_renderer = Mock()
    host._setup_ai_editing = Mock()

    FileOpsMixin._load_edit_project(host, path)

    timeline_panel.set_timeline_sequence.assert_not_called()
    timeline_panel.clear.assert_called_once()
    timeline_panel.show_provisional_timeline.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# 关闭前保存确认矩阵
# ═══════════════════════════════════════════════════════════════════════


def test_has_unsaved_changes_matrix() -> None:
    no_project = make_host(_current_edit_project=None)
    assert FileOpsMixin._has_unsaved_changes(no_project) is False

    untitled = make_host(_current_edit_project=object(),
                         _current_edit_project_filepath=None)
    assert FileOpsMixin._has_unsaved_changes(untitled) is True  # 从未保存过

    dirty = make_host(_current_edit_project=object(),
                      _current_edit_project_filepath="/tmp/p.topos",
                      edit_project_dirty=True)
    assert FileOpsMixin._has_unsaved_changes(dirty) is True

    clean = make_host(_current_edit_project=object(),
                      _current_edit_project_filepath="/tmp/p.topos",
                      edit_project_dirty=False)
    assert FileOpsMixin._has_unsaved_changes(clean) is False


def test_confirm_close_without_changes_proceeds_without_dialog(msgbox) -> None:
    host = make_host(_current_edit_project=None)
    assert FileOpsMixin._confirm_save_before_close(host) is True
    assert msgbox.calls == []


def test_confirm_close_save_success_proceeds(msgbox) -> None:
    from PyQt6.QtWidgets import QMessageBox

    host = make_host(_current_edit_project=object(),
                     _current_edit_project_filepath="/tmp/p.topos",
                     edit_project_dirty=True)
    saved = Mock(name="save_file")
    host.save_file = saved  # 保存成功后 dirty 清零
    saved.side_effect = lambda: setattr(host, "edit_project_dirty", False)
    msgbox.state["question_result"] = QMessageBox.StandardButton.Save

    assert FileOpsMixin._confirm_save_before_close(host) is True
    saved.assert_called_once()


def test_confirm_close_save_failure_aborts_close(msgbox) -> None:
    """保存抛异常 → 让用户重新决定，而不是静默丢弃。"""
    from PyQt6.QtWidgets import QMessageBox

    host = make_host(_current_edit_project=object(),
                     _current_edit_project_filepath="/tmp/p.topos",
                     edit_project_dirty=True)
    host.save_file = Mock(side_effect=OSError("disk full"))
    msgbox.state["question_result"] = QMessageBox.StandardButton.Save

    assert FileOpsMixin._confirm_save_before_close(host) is False


def test_confirm_close_save_as_cancelled_aborts_close(msgbox) -> None:
    """保存后仍处于未保存态（用户取消了"另存为"）→ 中止关闭。"""
    from PyQt6.QtWidgets import QMessageBox

    untitled = make_host(_current_edit_project=object(),
                         _current_edit_project_filepath=None)
    untitled.save_file = Mock(name="save_file")  # no-op：仍无路径
    msgbox.state["question_result"] = QMessageBox.StandardButton.Save

    assert FileOpsMixin._confirm_save_before_close(untitled) is False


def test_confirm_close_discard_marks_user_discard(msgbox) -> None:
    from PyQt6.QtWidgets import QMessageBox

    host = make_host(_current_edit_project=object(),
                     _current_edit_project_filepath="/tmp/p.topos",
                     edit_project_dirty=True)
    msgbox.state["question_result"] = QMessageBox.StandardButton.Discard

    assert FileOpsMixin._confirm_save_before_close(host) is True
    assert host._user_discarded_changes is True  # AUD-2800：关闭末尾据此跳过回写


def test_confirm_close_cancel_aborts(msgbox) -> None:
    from PyQt6.QtWidgets import QMessageBox

    host = make_host(_current_edit_project=object(),
                     _current_edit_project_filepath="/tmp/p.topos",
                     edit_project_dirty=True)
    msgbox.state["question_result"] = QMessageBox.StandardButton.Cancel

    assert FileOpsMixin._confirm_save_before_close(host) is False
    assert host._user_discarded_changes is False


# ═══════════════════════════════════════════════════════════════════════
# 关闭时持久化（AUD-2800 Discard 语义）
# ═══════════════════════════════════════════════════════════════════════


def test_persist_before_close_skips_save_after_user_discard() -> None:
    host = make_host(_user_discarded_changes=True)
    host._flush_composite_to_edit_before_leave = Mock(name="flush")

    FileOpsMixin._persist_active_project_before_close(host)

    host._flush_composite_to_edit_before_leave.assert_called_once_with()
    host.save_router.save.assert_not_called()  # 被丢弃的脏状态不得写回主工程


def test_persist_before_close_save_failure_does_not_raise() -> None:
    """关闭路径的保存失败只记日志，停机流程必须继续（可确定性收尾）。"""
    host = make_host()
    host._flush_composite_to_edit_before_leave = Mock(name="flush")
    host.save_router.save.side_effect = OSError("readonly filesystem")

    FileOpsMixin._persist_active_project_before_close(host)  # 不抛出即通过

    host.save_router.save.assert_called_once_with()


def test_persist_before_close_survives_composite_flush_failure() -> None:
    host = make_host()
    host._flush_composite_to_edit_before_leave = Mock(
        side_effect=RuntimeError("composite teardown boom"))

    FileOpsMixin._persist_active_project_before_close(host)

    host.save_router.save.assert_called_once_with()


def test_clear_all_dirty_states_resets_every_flag() -> None:
    host = make_host(edit_project_dirty=True, color_session_dirty=True,
                     composite_dirty=True, deliver_dirty=True)

    FileOpsMixin._clear_all_dirty_states(host)

    assert host.edit_project_dirty is False
    assert host.color_session_dirty is False
    assert host.composite_dirty is False
    assert host.deliver_dirty is False


# ═══════════════════════════════════════════════════════════════════════
# new_project / close_project 工作模式路由
# ═══════════════════════════════════════════════════════════════════════


def test_new_project_aborts_when_active_project_close_cancelled() -> None:
    host = make_host(_current_edit_project=object())
    host.close_results = [False]
    host._create_video_project = Mock(name="_create_video_project")

    FileOpsMixin.new_project(host)

    host._create_video_project.assert_not_called()
    assert host.adjustments_reset_called == [False]
    host.status_bar.showMessage.assert_not_called()


def test_new_project_video_mode_creates_timeline_document() -> None:
    host = make_host(_edit_available=True)
    host._get_work_mode = lambda: "video"
    host._create_video_project = Mock(name="_create_video_project")

    FileOpsMixin.new_project(host)

    host._create_video_project.assert_called_once()
    assert host.adjustments_reset_called == [False]


def test_new_project_image_mode_resets_grading_state() -> None:
    host = make_host(_edit_available=True)
    host._get_work_mode = lambda: "image"

    FileOpsMixin.new_project(host)

    assert host.adjustments_reset_called == [True]
    host.status_bar.showMessage.assert_called_once()


def test_close_project_defers_to_active_motion_graphic_editor(monkeypatch) -> None:
    """MG 编辑器激活时 Cmd+W 关最前窗口（编辑器），不动工程。"""
    editor = SimpleNamespace(close=Mock(name="editor.close"))
    monkeypatch.setattr(fc, "active_motion_graphic_editor", lambda: editor)
    host = make_host(_current_edit_project=object())

    assert FileOpsMixin.close_project(host) is True
    editor.close.assert_called_once()
    assert host.unload_calls == []  # 工程侧无任何卸载动作


def test_close_project_clears_pre_project_media_session(monkeypatch) -> None:
    """无工程、仅有媒体会话：不保存直接卸载并复位窗口标题/MG 根。"""
    monkeypatch.setattr(fc, "active_motion_graphic_editor", lambda: None)
    host = make_host(current_file_path="/tmp/clip.mp4", current_file_is_video=True)

    assert FileOpsMixin.close_project(host) is True
    assert host.unload_calls == [{"save_before_unload": False}]
    assert host.current_file_path is None
    assert host.current_file_is_video is False
    host.save_router.clear_cache.assert_called_once()
    assert host.mg_roots == [None]
    assert host.titles == ["Topos"]


def test_close_project_without_anything_is_noop_true(monkeypatch) -> None:
    monkeypatch.setattr(fc, "active_motion_graphic_editor", lambda: None)
    host = make_host()

    assert FileOpsMixin.close_project(host) is True
    assert host.unload_calls == []


# ═══════════════════════════════════════════════════════════════════════
# 媒体分发 / 拖放（AUD-2801 统一路由）
# ═══════════════════════════════════════════════════════════════════════


def test_open_media_path_rejects_video_on_color_page(monkeypatch) -> None:
    monkeypatch.setattr(fc, "_is_video_file", lambda p: True)
    host = make_host(current_page="Color")
    rejected: list[str] = []
    host._reject_standalone_video_for_color = rejected.append

    FileOpsMixin._open_media_path(host, "/tmp/a.mp4")

    assert rejected == ["/tmp/a.mp4"]
    assert host.load_video_calls == []


def test_open_media_path_loads_video_on_edit_page(monkeypatch) -> None:
    monkeypatch.setattr(fc, "_is_video_file", lambda p: True)
    host = make_host(current_page="Edit")

    FileOpsMixin._open_media_path(host, "/tmp/a.mp4")

    assert host.load_video_calls == [("/tmp/a.mp4", {})]


def test_open_media_path_opens_adjacent_standalone_project(
        tmp_path, monkeypatch) -> None:
    """图像已有自己的邻接 .topos 工程 → 直接打开工程，不新建空会话。"""
    monkeypatch.setattr(fc, "_is_video_file", lambda p: False)
    image = tmp_path / "graded.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    project = _make_topos(str(tmp_path / "graded.topos"), "standalone_image")
    host = make_host()
    host.save_router = SimpleNamespace(
        _get_project_path_for_file=lambda p: project,
    )

    FileOpsMixin._open_media_path(host, str(image))

    assert host.open_project_calls == [project]
    assert host.load_image_calls == []


def test_open_media_path_falls_back_to_load_image_for_new_media(
        tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fc, "_is_video_file", lambda p: False)
    image = tmp_path / "new.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    host = make_host()
    host.save_router = SimpleNamespace(
        _get_project_path_for_file=lambda p: str(tmp_path / "new.topos"),
    )

    FileOpsMixin._open_media_path(host, str(image))

    assert host.load_image_calls == [(str(image), {})]
    assert host.open_project_calls == []


class _FakeMimeData:
    def __init__(self, urls: bool, local_files: list[str] | None = None) -> None:
        self._urls = urls
        self._files = local_files or []

    def hasUrls(self) -> bool:
        return self._urls

    def urls(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(toLocalFile=lambda f=f: f) for f in self._files]


class _FakeDragEvent:
    def __init__(self, mime: _FakeMimeData) -> None:
        self._mime = mime
        self.actions: list[str] = []

    def mimeData(self) -> _FakeMimeData:
        return self._mime

    def acceptProposedAction(self) -> None:
        self.actions.append("accept")

    def ignore(self) -> None:
        self.actions.append("ignore")


def test_drag_enter_accepts_url_drops_and_ignores_others() -> None:
    host = make_host()
    with_urls = _FakeDragEvent(_FakeMimeData(urls=True, local_files=["/tmp/a.png"]))
    FileOpsMixin.dragEnterEvent(host, with_urls)
    assert with_urls.actions == ["accept"]

    no_urls = _FakeDragEvent(_FakeMimeData(urls=False))
    FileOpsMixin.dragEnterEvent(host, no_urls)
    assert no_urls.actions == ["ignore"]


def test_drop_event_routes_first_file_through_media_router(monkeypatch) -> None:
    """AUD-2801 回归锁：拖入视频/非媒体文件必须走统一 Open Media 路由。"""
    host = make_host()
    opened: list[str] = []
    host._open_media_path = opened.append
    drop = _FakeDragEvent(_FakeMimeData(urls=True, local_files=["/tmp/b.mp4"]))

    FileOpsMixin.dropEvent(host, drop)

    assert opened == ["/tmp/b.mp4"]


def test_drop_event_without_files_is_noop() -> None:
    host = make_host()
    host._open_media_path = Mock(name="_open_media_path")
    drop = _FakeDragEvent(_FakeMimeData(urls=True, local_files=[]))

    FileOpsMixin.dropEvent(host, drop)

    host._open_media_path.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# 最近文件 / 菜单接线
# ═══════════════════════════════════════════════════════════════════════


def test_add_to_recent_files_prepends_dedupes_and_caps() -> None:
    config = _StubConfig({
        "recent_files": ["/tmp/old2", "/tmp/a.png", "/tmp/old3"],
        "max_recent_files": 2,
    })
    host = make_host(config_manager=config)
    rebuilds = []
    host._rebuild_recent_menu = lambda: rebuilds.append(1)

    FileOpsMixin._add_to_recent_files(host, "/tmp/a.png")  # 已在列表中（去重提前）

    recent = config.get("recent_files")
    assert recent[0] == "/tmp/a.png"
    assert len(recent) == 2  # 上限截断（超出上限的 old3 被裁掉）
    assert "/tmp/old3" not in recent
    assert rebuilds == [1]  # 菜单重建被触发


def test_add_to_recent_files_ignores_empty_path() -> None:
    config = _StubConfig({"recent_files": ["/tmp/x"]})
    host = make_host(config_manager=config)
    host._rebuild_recent_menu = Mock(name="_rebuild_recent_menu")

    FileOpsMixin._add_to_recent_files(host, "")

    assert config.get("recent_files") == ["/tmp/x"]
    host._rebuild_recent_menu.assert_not_called()


def test_open_recent_file_opens_existing_path(tmp_path) -> None:
    media = tmp_path / "recent.png"
    media.write_bytes(b"x")
    host = make_host()
    opened: list[str] = []
    host._open_media_path = opened.append
    action = SimpleNamespace(data=lambda: str(media))
    host.sender = lambda: action

    FileOpsMixin._open_recent_file(host)

    assert opened == [str(media)]


def test_open_recent_file_ignores_missing_file() -> None:
    host = make_host()
    host._open_media_path = Mock(name="_open_media_path")
    action = SimpleNamespace(data=lambda: "/gone/file.png")
    host.sender = lambda: action

    FileOpsMixin._open_recent_file(host)

    host._open_media_path.assert_not_called()


def test_open_recent_file_without_sender_is_noop() -> None:
    host = make_host()
    host._open_media_path = Mock(name="_open_media_path")
    host.sender = lambda: None

    FileOpsMixin._open_recent_file(host)

    host._open_media_path.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# 元数据异步加载器（R0-7）
# ═══════════════════════════════════════════════════════════════════════


def _wait_for(predicate, timeout_ms: int = 5000) -> None:
    from PyQt6.QtTest import QTest

    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(20)
        waited += 20


def test_metadata_loader_serves_cache_hit_without_new_worker(
        qapp, tmp_path, monkeypatch) -> None:
    media = tmp_path / "m.png"
    media.write_bytes(b"x")
    get_metadata = Mock(name="FileIO.get_metadata", return_value={"ISO": "100"})
    monkeypatch.setattr(fc.FileIO, "get_metadata", get_metadata)

    loader = fc._MetadataLoader(None)
    received: list[tuple[str, dict]] = []
    loader.ready.connect(lambda path, meta: received.append((path, meta)))
    try:
        loader.request(str(media))
        fc.QThreadPool.globalInstance().waitForDone(5000)
        _wait_for(lambda: bool(received))
        assert received == [(str(media), {"ISO": "100"})]

        loader.request(str(media))  # 缓存命中：同步回放，不再入线程池
        assert received == [(str(media), {"ISO": "100"})] * 2
        assert get_metadata.call_count == 1
    finally:
        loader.deleteLater()


def test_metadata_loader_dedupes_inflight_requests(qapp, tmp_path, monkeypatch) -> None:
    """同一路径在途时只派发一个 worker（exiftool 子进程不重复拉起）。"""
    media = tmp_path / "dup.png"
    media.write_bytes(b"x")
    started: list[object] = []
    monkeypatch.setattr(
        fc.QThreadPool.globalInstance(), "start",
        lambda runnable: started.append(runnable),
    )

    loader = fc._MetadataLoader(None)
    try:
        loader.request(str(media))
        loader.request(str(media))
        loader.request(str(media))
    finally:
        loader.deleteLater()

    assert len(started) == 1


def test_metadata_worker_exception_falls_back_to_empty_metadata(
        qapp, tmp_path, monkeypatch) -> None:
    media = tmp_path / "bad.png"
    media.write_bytes(b"x")

    def _boom(_path: str) -> dict:
        raise RuntimeError("exiftool crashed")

    monkeypatch.setattr(fc.FileIO, "get_metadata", _boom)

    loader = fc._MetadataLoader(None)
    received: list[tuple[str, tuple, dict]] = []
    worker = fc._MetadataWorker(str(media), ("k",), loader)
    worker.signals.ready.connect(lambda *args: received.append(args))
    try:
        worker.run()  # 同步执行，异常路径不得外泄
    finally:
        loader.deleteLater()

    assert received == [(str(media), ("k",), {})]


def test_on_metadata_ready_only_refreshes_current_file() -> None:
    host = make_host(current_file_path="/tmp/now.png")
    update = Mock(name="update_metadata")
    host.info_panel = SimpleNamespace(update_metadata=update)

    FileOpsMixin._on_metadata_ready(host, "/tmp/other.png", {"ISO": "400"})
    update.assert_not_called()

    FileOpsMixin._on_metadata_ready(host, "/tmp/now.png", {"ISO": "400"})
    update.assert_called_once_with({"ISO": "400"})


# ═══════════════════════════════════════════════════════════════════════
# GPU 渲染线程停机（P1-THREAD-01 推迟销毁）
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def leaked_gpu_objects() -> list:
    """快照 _LEAKED_GPU_OBJECTS，测试结束后裁剪回原状（模块级状态隔离）。"""
    before = len(fc._LEAKED_GPU_OBJECTS)
    yield fc._LEAKED_GPU_OBJECTS
    del fc._LEAKED_GPU_OBJECTS[before:]


def test_stop_gpu_renderer_deferred_defers_on_timeout(leaked_gpu_objects) -> None:
    renderer = SimpleNamespace(stop=lambda timeout_ms=0: False)

    fc._stop_gpu_renderer_deferred(renderer, timeout_ms=100)

    assert leaked_gpu_objects[-1] is renderer  # 超时：销毁推迟到进程退出


def test_stop_gpu_renderer_deferred_clean_stop_not_leaked(leaked_gpu_objects) -> None:
    before = len(leaked_gpu_objects)
    renderer = SimpleNamespace(stop=lambda timeout_ms=0: True)

    fc._stop_gpu_renderer_deferred(renderer, timeout_ms=100)

    assert len(leaked_gpu_objects) == before


def test_stop_gpu_renderer_deferred_stop_exception_skips(leaked_gpu_objects) -> None:
    before = len(leaked_gpu_objects)

    class _Boom:
        def stop(self, timeout_ms: int = 0) -> bool:
            raise ValueError("GL cleanup exploded")

    fc._stop_gpu_renderer_deferred(_Boom(), timeout_ms=100)

    assert len(leaked_gpu_objects) == before


def test_stop_gpu_renderer_deferred_typeerror_falls_back_to_plain_stop() -> None:
    class _LegacyStop:
        def __init__(self) -> None:
            self.plain_calls = 0

        def stop(self) -> bool:  # 不接受 timeout 参数
            self.plain_calls += 1
            return True

    legacy = _LegacyStop()
    fc._stop_gpu_renderer_deferred(legacy, timeout_ms=100)
    assert legacy.plain_calls == 1
    assert legacy not in fc._LEAKED_GPU_OBJECTS


# ═══════════════════════════════════════════════════════════════════════
# 合成图相对路径绝对化（纯函数）
# ═══════════════════════════════════════════════════════════════════════


def test_absolutize_rewrites_existing_relative_image_paths(tmp_path) -> None:
    img = tmp_path / "node.png"
    img.write_bytes(b"x")
    graph = {"nodes": [{"type": "image_input",
                        "parameters": {"file_path": "node.png"}}]}

    result = FileOpsMixin._absolutize_composite_paths(graph, str(tmp_path))

    assert result["nodes"][0]["parameters"]["file_path"] == str(img)


def test_absolutize_keeps_missing_and_absolute_paths(tmp_path) -> None:
    graph = {"nodes": [
        {"type": "image_input", "parameters": {"file_path": "missing.png"}},
        {"type": "image_input", "parameters": {"file_path": "/abs/keep.png"}},
        {"type": "color_input", "parameters": {"file_path": "node.png"}},
    ]}

    result = FileOpsMixin._absolutize_composite_paths(graph, str(tmp_path))

    params = [n["parameters"]["file_path"] for n in result["nodes"]]
    assert params == ["missing.png", "/abs/keep.png", "node.png"]


def test_absolutize_noop_on_empty_inputs(tmp_path) -> None:
    assert FileOpsMixin._absolutize_composite_paths({}, str(tmp_path)) == {}
    assert FileOpsMixin._absolutize_composite_paths({"nodes": []}, "") == {"nodes": []}


# ═══════════════════════════════════════════════════════════════════════
# MG preset 工程根同步
# ═══════════════════════════════════════════════════════════════════════


def test_sync_mg_preset_project_root_delegates_and_tolerates_missing_module(
        monkeypatch) -> None:
    calls: list[str | None] = []
    import src.features.teaching_animation.presets.paths as paths_mod

    monkeypatch.setattr(paths_mod, "set_active_project_file", calls.append)
    host = make_host()

    FileOpsMixin._sync_mg_preset_project_root(host, "/tmp/proj.topos")
    FileOpsMixin._sync_mg_preset_project_root(host, None)

    assert calls == ["/tmp/proj.topos", None]
