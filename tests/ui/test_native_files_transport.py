"""Transport 层测试：D1 换轨后数据面走 facade/本地直读，媒体也走本地共享 provider。

facade 部分：注入 tmp 配置根的真实 catalog/facade（合成素材），不启动
任何 HTTP 服务——数据路由对旧服务启停无感是本批的验收点。缩略图/胶片条不依赖 HTTP 服务，全部使用本地后端。
"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from PyQt6.QtCore import QTimer
from pytestqt.qtbot import QtBot

from src.app.ui.native_files_transport import NativeFilesTransport
from src.features.files.contracts import identity_key
from src.features.files.repository import FilesDataLocator
from src.features.files.repository.catalog import FilesCatalog
from src.features.files.services.facade import FilesFacade
from src.features.files.services.query import FilesQueryService


# ---------------------------------------------------------------------------
# facade 环境（数据面）
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path)
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    catalog.open()
    library = catalog.register_library(tmp_path / "lib")
    lib = tmp_path / "lib"
    lib.mkdir()
    yield SimpleNamespace(root=tmp_path, catalog=catalog, library=library, lib=lib)
    catalog.close()


class GatedQuery(FilesQueryService):
    """按 key 闸住查询：让"旧请求在途"可控，验证代际作废的确定性。"""

    def __init__(self, catalog):
        super().__init__(catalog)
        self.gates: dict[str, threading.Event] = {}
        self.entered: list[str] = []

    def list_directory(self, request, **kwargs):
        key = f"list:{identity_key(request.path)}"
        self.entered.append(key)
        gate = self.gates.get(key)
        if gate is not None:
            gate.wait(5)
        return super().list_directory(request, **kwargs)

    def search(self, query, **kwargs):
        key = f"search:{query}"
        self.entered.append(key)
        gate = self.gates.get(key)
        if gate is not None:
            gate.wait(5)
        return super().search(query, **kwargs)

    def tree(self, path):
        key = f"tree:{identity_key(path)}"
        self.entered.append(key)
        gate = self.gates.get(key)
        if gate is not None:
            gate.wait(5)
        return super().tree(path)


def make_transport(env, *, query=None):
    facade = FilesFacade(env.catalog, max_workers=1, query=query)
    return NativeFilesTransport(lambda: "", None, facade=facade)


def _seed(env, name, payload=b"data"):
    asset = env.lib / name
    asset.write_bytes(payload)
    return asset


# ---------------------------------------------------------------------------
# 目录内容 / 目录树
# ---------------------------------------------------------------------------

def test_directory_roundtrip_typed_entries_with_overlay(qtbot, env):
    asset = _seed(env, "a.png")
    env.catalog.upsert_asset(env.library.library_id, asset, size=4)
    env.catalog.patch_user_fields(env.library.library_id, asset, rating=5)
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.loaded, timeout=8000) as received:
            transport.load_directory(str(env.lib))
        path_arg, entries = received.args
        assert path_arg == str(env.lib)
        assert entries[0].name == "a.png"
        assert entries[0].kind == "image"
        assert entries[0].user.rating == 5  # catalog 叠加（库已固定）
        assert not transport.active_task_jobs()
    finally:
        transport.shutdown()


def test_failed_directory_never_publishes_entries(qtbot, env):
    transport = make_transport(env)
    loaded = []
    transport.loaded.connect(lambda *args: loaded.append(args))
    try:
        with qtbot.waitSignal(transport.failed, timeout=8000) as received:
            transport.load_directory(str(env.root / "ghost"))
        assert received.args[0] == str(env.root / "ghost")
        assert "目录不存在" in received.args[1]
        assert loaded == []
    finally:
        transport.shutdown()


def test_directory_switch_drops_late_result(qtbot, env):
    """[FR-09] 新导航作废在途旧响应：迟到的旧目录绝不覆盖新视图。"""
    other = env.root / "other"
    other.mkdir()
    query = GatedQuery(env.catalog)
    gate = threading.Event()
    query.gates[f"list:{identity_key(str(env.lib))}"] = gate
    transport = make_transport(env, query=query)
    loaded = []
    transport.loaded.connect(lambda path, _: loaded.append(path))
    try:
        transport.load_directory(str(env.lib))  # worker 闸在旧目录
        qtbot.waitUntil(lambda: bool(query.entered), timeout=8000)
        with qtbot.waitSignal(transport.loaded, timeout=8000):
            transport.load_directory(str(other))  # cancel() 自增导航代际
            gate.set()                            # 放行旧请求：投递时已被作废
        qtbot.wait(150)
        assert loaded == [str(other)]
    finally:
        gate.set()
        transport.shutdown()


@pytest.mark.parametrize("old_directory_exists", [True, False])
def test_directory_switch_discards_result_already_queued_to_qt(
    qtbot: QtBot, env: SimpleNamespace, old_directory_exists: bool,
) -> None:
    old = env.lib if old_directory_exists else env.root / "missing"
    query = GatedQuery(env.catalog)
    gate = threading.Event()
    query.gates[f"list:{identity_key(str(old))}"] = gate
    facade = FilesFacade(env.catalog, max_workers=1, query=query)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    loaded = []
    failed = []
    pages = []
    transport.loaded.connect(lambda path, _: loaded.append(path))
    transport.failed.connect(lambda path, _: failed.append(path))
    transport.page_meta.connect(lambda path, *_: pages.append(path))
    try:
        transport.load_directory(str(old))
        idle = threading.Event()
        facade.when_idle(idle.set)
        gate.set()
        # Do not pump Qt until the newer request has invalidated the posted reply.
        assert idle.wait(3)
        with qtbot.waitSignal(transport.loaded, timeout=3000):
            transport.load_directory(str(env.lib))
        assert loaded == pages == [str(env.lib)]
        assert failed == []
    finally:
        gate.set()
        transport.shutdown()


def test_linked_files_panels_follow_latest_directory_without_echo(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.ui.files_panel import FilesPanel
    from src.app.ui.native_files_panel import NativeFilesPanel

    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
    browsable = NativeFilesPanel.is_browsable_path

    def allow_test_directories(panel: NativeFilesPanel, path: str) -> bool:
        # macOS places pytest's temporary media under /private/var.
        return path.startswith(f"{tmp_path}/") or browsable(panel, path)

    monkeypatch.setattr(NativeFilesPanel, "is_browsable_path", allow_test_directories)
    first_dir, last_dir = tmp_path / "first", tmp_path / "last"
    first_dir.mkdir()
    last_dir.mkdir()
    files = FilesPanel(include_plugins=False)
    library = FilesPanel(include_plugins=False)
    for panel in (files, library):
        qtbot.addWidget(panel)
    files.directory_navigation_requested.connect(library.follow_directory)
    library.directory_navigation_requested.connect(files.follow_directory)
    requested: list[str] = []
    files.directory_navigation_requested.connect(requested.append)
    library.directory_navigation_requested.connect(requested.append)
    try:
        files.set_root_path(str(first_dir))
        library.set_root_path(str(last_dir))
        qtbot.waitUntil(lambda: files._files_root_path == library._files_root_path == str(last_dir))
        assert requested == [str(first_dir), str(last_dir)]
    finally:
        for panel in (files, library):
            panel.shutdown()


def test_data_routes_never_touch_network(qtbot, env):
    """数据面与旧 HTTP 服务完全解耦：网络栈被换成哨兵也能浏览/搜索。"""
    _seed(env, "solo.png")
    env.catalog.upsert_asset(env.library.library_id, env.lib / "solo.png")
    transport = make_transport(env)
    try:

        class _NoNetwork:
            def get(self, *_args):
                raise AssertionError("数据路由不得发起 HTTP 请求")

            def post(self, *_args):
                raise AssertionError("数据路由不得发起 HTTP 请求")

        transport._network = _NoNetwork()  # noqa: SLF001 - 哨兵注入
        with qtbot.waitSignal(transport.loaded, timeout=8000):
            transport.load_directory(str(env.lib))
        with qtbot.waitSignal(transport.search_loaded, timeout=8000):
            transport.search_library("solo")
        with qtbot.waitSignal(transport.capabilities_loaded, timeout=8000):
            transport.load_capabilities()
    finally:
        transport.shutdown()


def test_tree_children_dedup_and_queue_bound(qtbot, env):
    (env.lib / "sub1").mkdir()
    (env.lib / "sub2").mkdir()
    _seed(env, "file.png")  # 文件不进树
    gate = threading.Event()
    query = GatedQuery(env.catalog)
    query.gates[f"tree:{identity_key(f'{env.lib}/queue-0')}"] = gate
    transport = make_transport(env, query=query)
    try:
        with qtbot.waitSignal(transport.tree_loaded, timeout=8000) as received:
            transport.load_tree(str(env.lib))
            transport.load_tree(str(env.lib))  # 在途去重
        assert received.args[0] == str(env.lib)
        assert [item["name"] for item in received.args[1]] == ["sub1", "sub2"]
        assert str(env.lib) not in transport._tree_pending_paths

        failed = []
        transport.tree_failed.connect(lambda *args: failed.append(args))
        for index in range(200):
            transport.load_tree(f"{env.lib}/queue-{index}")
        # worker 被闸住：在途集合（1 在跑 + 127 排队）恰好封顶，超出即时拒绝
        assert len(transport._tree_pending_paths) == transport.MAX_TREE_QUEUE
        assert len(failed) == 200 - transport.MAX_TREE_QUEUE
    finally:
        gate.set()
        transport.shutdown()


def test_tree_missing_directory_reports_empty(qtbot, env):
    """幽灵目录没有子节点：空列表是如实结果（不冒充错误）。"""
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.tree_loaded, timeout=8000) as received:
            transport.load_tree(str(env.root / "ghost"))
        assert received.args == [str(env.root / "ghost"), []]
    finally:
        transport.shutdown()


def test_macos_mount_snapshot_lists_data_volumes_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.ui import native_files_locations as locations
    from src.shared.io import macos_mount_table

    monkeypatch.setattr(locations, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(locations.QStorageInfo, "mountedVolumes",
                        lambda: pytest.fail("must not probe mounted disks"))
    monkeypatch.setattr(macos_mount_table, "cached_mounts", lambda: (
        ("/", b"/dev/disk1s1"),
        ("/System/Volumes/Data", b"/dev/disk1s2"),
        ("/Volumes/USB", b"/dev/disk2s1"),
        ("/Volumes/Archive", b"/dev/disk3s1"),
    ))
    assert [mount.name for mount in locations.mounted_data_volumes()] == ["Archive", "USB"]


def test_transport_reads_mount_snapshot_off_ui_thread(
    qtbot: QtBot, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.ui import native_files_transport
    from src.app.ui.native_files_locations import MountedVolume

    worker_threads: list[int] = []
    mount = MountedVolume("/Volumes/USB", "USB", b"device")

    def read_mounts() -> tuple[MountedVolume, ...]:
        worker_threads.append(threading.get_ident())
        return (mount,)

    monkeypatch.setattr(native_files_transport, "mounted_data_volumes", read_mounts)
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.volumes_loaded) as result:
            transport.load_volumes()
            transport.load_volumes()
        assert result.args == [(mount,)]
        assert len(worker_threads) == 1 and worker_threads[0] != threading.get_ident()
    finally:
        transport.shutdown()


def test_visible_sidebar_updates_when_drive_is_removed(
    qtbot: QtBot, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.ui import native_files_transport
    from src.app.ui.native_files_locations import MountedVolume
    from src.app.ui.native_files_panel import NativeFilesPanel

    usb = MountedVolume("/Volumes/USB", "USB", b"device")
    mounts = (usb,)
    monkeypatch.setattr(native_files_transport, "mounted_data_volumes", lambda: mounts)
    transport = make_transport(env)
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.resize(860, 520)
    tree = panel.locations
    tree._volume_poll.setInterval(25)
    tree.volumes_requested.connect(transport.load_volumes)
    transport.volumes_loaded.connect(tree.set_volumes)
    try:
        panel.show()
        qtbot.waitUntil(lambda: usb.path in tree._nodes)
        mounts = ()
        qtbot.waitUntil(lambda: usb.path not in tree._nodes)
        assert tree.item_model.rowCount() == 1
        panel.hide()
        assert not tree._volume_poll.isActive()
    finally:
        transport.shutdown()


# ---------------------------------------------------------------------------
# 元数据 / 文件任务
# ---------------------------------------------------------------------------

def test_metadata_patch_publishes_snapshot_and_failures(qtbot, env):
    pinned = _seed(env, "pinned.png")
    env.catalog.upsert_asset(env.library.library_id, pinned, size=4)
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.metadata_snapshot, timeout=8000) as snap:
            transport.patch_metadata([str(pinned)], {"tags": "夜, 航拍"})
        snapshot = snap.args[0]
        assert sorted(snapshot[str(pinned)]["tags"]) == ["夜", "航拍"]

        outsider = env.root / "outside.png"
        with qtbot.waitSignal(transport.metadata_failed, timeout=8000) as received:
            transport.patch_metadata([str(outsider)], {"rating": 3})
        assert received.args[0] == [str(outsider)]

        outsider.write_bytes(b"x")
        with qtbot.waitSignal(transport.metadata_snapshot, timeout=8000) as snap:
            transport.patch_metadata([str(outsider)], {"rating": 3})
        assert snap.args[0][str(outsider)]["rating"] == 3
        with qtbot.waitSignal(transport.loaded, timeout=8000) as loaded:
            transport.load_directory(str(env.root))
        assert next(entry for entry in loaded.args[1] if entry.path == str(outsider)).user.rating == 3
    finally:
        transport.shutdown()


def test_metadata_tags_clear_roundtrip(qtbot, env):
    asset = _seed(env, "clear.png")
    env.catalog.upsert_asset(env.library.library_id, asset, size=4)
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.metadata_snapshot, timeout=8000):
            transport.patch_metadata([str(asset)], {"tags": "a, b"})
        with qtbot.waitSignal(transport.metadata_snapshot, timeout=8000) as snap:
            transport.patch_metadata([str(asset)], {"tags": ""}, tags_op="clear")
        assert snap.args[0][str(asset)]["tags"] == []
        assert env.catalog.tags_of(env.library.library_id, str(asset)) == []
    finally:
        transport.shutdown()


def test_rapid_metadata_writes_are_applied_in_click_order(qtbot, env, monkeypatch):
    asset = _seed(env, "rapid.png")
    env.catalog.upsert_asset(env.library.library_id, asset)
    facade = FilesFacade(env.catalog, max_workers=2)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    entered = threading.Event()
    release = threading.Event()
    patch_metadata = facade.metadata.patch_metadata

    def gated_patch(paths, patch, **kwargs):
        if patch == {"rating": 5}:
            entered.set()
            assert release.wait(5)
        return patch_metadata(paths, patch, **kwargs)

    monkeypatch.setattr(facade.metadata, "patch_metadata", gated_patch)
    try:
        applied = []
        transport.metadata_applied.connect(lambda _paths, patch, _msg: applied.append(patch))
        transport.patch_metadata([str(asset)], {"rating": 5})
        qtbot.waitUntil(entered.is_set)
        transport.patch_metadata([str(asset)], {"color": 4})
        transport.patch_metadata([str(asset)], {"rating": 2})
        release.set()
        qtbot.waitUntil(lambda: len(applied) == 3)
        assert applied == [{"rating": 5}, {"color": 4}, {"rating": 2}]
        row = env.catalog.asset(env.library.library_id, asset)
        assert (row["rating"], row["color"]) == (2, 4)
    finally:
        release.set()
        transport.shutdown()


def test_rejected_metadata_is_not_reported_as_saved(qtbot, env):
    asset = _seed(env, "rating.png")
    env.catalog.upsert_asset(env.library.library_id, asset)
    transport = make_transport(env)
    try:
        with qtbot.assertNotEmitted(transport.metadata_applied):
            with qtbot.waitSignal(transport.metadata_failed, timeout=8000) as rejected:
                transport.patch_metadata([str(asset)], {"rating": 10})
        assert "rating" in rejected.args[2]
        assert env.catalog.asset(env.library.library_id, asset)["rating"] == 0
    finally:
        transport.shutdown()


def test_task_rename_progress_and_itemized_result(qtbot, env):
    asset = _seed(env, "a.png")
    transport = make_transport(env)
    started = []
    transport.task_started.connect(started.append)
    try:
        with qtbot.waitSignal(transport.task_finished, timeout=8000) as received:
            transport.run_task("rename", [str(asset)],
                               options={"new_name": "b.png"})
        job = received.args[0]
        assert job["successes"] == [{"path": str(asset),
                                     "to": str(env.lib / "b.png")}]
        assert job["failures"] == [] and not job["cancelled"]
        assert (env.lib / "b.png").exists() and not asset.exists()
        assert started and started[0]["completed"] == 0
        assert started[-1]["completed"] == started[-1]["total"] == 1
        assert transport.active_task_jobs() == []
    finally:
        transport.shutdown()


def test_task_conflict_is_itemized_not_silent(qtbot, env):
    a = _seed(env, "first.png")
    b = _seed(env, "second.png")
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.task_finished, timeout=8000) as received:
            transport.run_task("rename", [str(a), str(b)],
                               options={"new_name": "same.png"})
        job = received.args[0]
        assert len(job["successes"]) == 1
        assert len(job["failures"]) == 1
        assert job["failures"][0]["code"] == "DEST_EXISTS"
        assert "已存在" in job["failures"][0]["error"]
    finally:
        transport.shutdown()


def test_task_mkdir_joins_name_option(qtbot, env):
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.task_finished, timeout=8000) as received:
            transport.run_task("mkdir", [str(env.lib)], options={"name": "新建"})
        assert received.args[0]["successes"]
        assert (env.lib / "新建").is_dir()

        missing = env.lib / "removed"
        with qtbot.waitSignal(transport.task_finished, timeout=8000) as failed:
            transport.run_task("mkdir", [str(missing)], options={"name": "子目录"})
        assert failed.args[0]["failures"]
        assert not missing.exists()
    finally:
        transport.shutdown()


def test_cancel_queued_mutation_before_worker_can_start(qtbot, env):
    asset = _seed(env, "queued.png")
    transport = make_transport(env)
    gate = threading.Event()
    try:
        transport._facade.run("gate", "gate", lambda: gate.wait(5), lambda _: None)
        transport.run_task("rename", [str(asset)], options={"new_name": "renamed.png"})
        job_id, = transport.active_task_jobs()
        transport.cancel_task(job_id)
        with qtbot.waitSignal(transport.task_finished, timeout=8000) as completed:
            gate.set()
        assert completed.args[0]["cancelled"]
        assert asset.exists() and not (env.lib / "renamed.png").exists()
    finally:
        gate.set()
        transport.shutdown()


# ---------------------------------------------------------------------------
# 常用位置 / 索引 / 搜索 / 集合
# ---------------------------------------------------------------------------

def test_libraries_add_rename_remove_roundtrip(qtbot, env):
    transport = make_transport(env)
    original = env.lib / "original.png"
    original.write_bytes(b"media")
    try:
        with qtbot.waitSignal(transport.library_changed, timeout=8000) as changed:
            transport.pin_library(str(env.lib))
        assert changed.args[0] is True
        with qtbot.waitSignal(transport.libraries_loaded, timeout=8000) as listed:
            transport.load_libraries()
        assert [item["path"] for item in listed.args[0]] == [str(env.lib)]
        assert listed.args[0][0]["library_id"] == env.library.library_id

        with qtbot.waitSignal(transport.library_changed, timeout=8000) as renamed:
            transport.rename_library(env.library.library_id, "Projects")
        assert renamed.args[0] is True
        record, = env.catalog.libraries()
        assert (record.library_id, record.name, record.root) == (
            env.library.library_id, "Projects", str(env.lib),
        )

        with qtbot.waitSignal(transport.library_changed, timeout=8000) as removed:
            transport.remove_library(env.library.library_id)
        assert removed.args[0] is True
        assert original.read_bytes() == b"media"
        with qtbot.waitSignal(transport.libraries_loaded, timeout=8000) as after:
            transport.load_libraries()
        assert after.args[0] == []

        with qtbot.waitSignal(transport.library_changed, timeout=8000) as unknown:
            transport.remove_library(env.library.library_id)
        assert unknown.args[0] is False
    finally:
        transport.shutdown()


def test_ai_scope_uses_selected_kind_and_folder_policy(qtbot, env, monkeypatch):
    from src.app.services import ai_library_analysis as adapter
    from src.features.files.services.ai_library import MediaEvidence
    from src.shared.ai_models.generation.provider_profiles import AiProviderProfile, ProfileRegistry

    root = env.lib.resolve()
    folder = root / "selected"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    image = folder / "source.png"
    for path in (image, nested / "nested.png", folder / "another.wav", root / "outside.png"):
        path.write_bytes(path.name.encode())
    registry = ProfileRegistry()
    for profile_id in ("global", "selected"):
        registry.add(AiProviderProfile(profile_id, profile_id, endpoint="http://localhost:9991/v1",
                                       model_id=profile_id, capabilities=("vision",)))
    registry.set_active_for_capability("vision", "global")
    monkeypatch.setattr(adapter, "_load_profiles", lambda: registry)
    monkeypatch.setattr(adapter.LibraryAnalyzer, "revision", property(lambda _: "test-revision"))
    calls: list[tuple[str, str, str]] = []

    def analyze(analyzer: adapter.LibraryAnalyzer, path: str, kind: str) -> tuple[MediaEvidence, ...]:
        if analyzer._mode == "basic":
            return ()
        route = analyzer._authorized_route("vision")
        calls.append((route.profile.profile_id, path, kind))
        return (MediaEvidence("annotated content"),)

    monkeypatch.setattr(adapter.LibraryAnalyzer, "__call__", analyze)
    library_id = env.library.library_id
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.ai_library_settings_saved) as saved:
            transport.save_ai_library_settings(library_id, "profiles", (), folder=str(folder),
                                               models={"image": "selected"}, kinds=("image",), recursive=False)
        assert saved.args == [""]
        with qtbot.waitSignal(transport.ai_library_settings_loaded) as loaded:
            transport.load_ai_library_settings(library_id, folder=str(folder))
        snapshot, = loaded.args
        assert snapshot["library_id"] == library_id and snapshot["folder"] == str(folder)
        assert snapshot["scope_policy"] == {"mode": "profiles", "approved_routes": [],
                                            "models": {"image": "selected"}, "kinds": ["image"],
                                            "recursive": False}
        assert snapshot["libraries"][0]["mode"] == "basic"
        assert {row["profile_id"] for row in snapshot["models"] if row["kind"] == "image"} == {"global", "selected"}

        for force, pending_only, call_count in ((False, False, 1), (False, True, 1), (True, False, 2)):
            with qtbot.waitSignal(transport.ai_library_index_status,
                                  check_params_cb=lambda state: not state.get("running")) as done:
                transport.start_ai_library_index(library_id, folder=str(folder), force=force,
                                                 only_unanalyzed=pending_only)
            assert "error" not in done.args[0]
            assert calls == [("selected", str(image), "image")] * call_count

        with qtbot.waitSignal(transport.ai_library_results) as found:
            transport.search_ai_library("scope", "annotated", library_id, folder=str(folder),
                                        kinds=("image",), recursive=False)
        assert (found.args[0], found.args[2]) == ("scope", "")
        assert [hit["path"] for hit in found.args[1]] == [str(image)]

        with qtbot.waitSignal(transport.ai_analysis_marks_loaded) as marked:
            transport.load_ai_analysis_marks(str(folder), [str(image), str(nested / "nested.png")])
        assert marked.args == [str(folder), [str(image)]]

        with qtbot.waitSignal(transport.ai_library_results) as pending:
            transport.search_ai_library("pending", "", library_id, folder=str(folder), kinds=("image",),
                                        recursive=True, only_unanalyzed=True)
        assert pending.args[2] == ""
        assert [hit["path"] for hit in pending.args[1]] == [str(nested / "nested.png")]

        with qtbot.waitSignal(transport.ai_library_settings_saved) as saved:
            transport.save_ai_library_settings(library_id, "profiles", (), models={"image": "global"},
                                               kinds=("image",), recursive=False)
        assert saved.args == [""]
        with qtbot.waitSignal(transport.ai_library_index_status,
                              check_params_cb=lambda state: not state.get("running")) as done:
            transport.start_ai_library_index(force=True)
        assert "error" not in done.args[0]
        assert calls[2:] == [("global", str(root / "outside.png"), "image")]
    finally:
        facade = transport._facade
        transport.shutdown()
        stopped = threading.Event()
        facade.when_idle(stopped.set)
        assert stopped.wait(2)


def test_ai_scope_reports_missing_model_or_unapproved_service_before_scanning(qtbot, env, monkeypatch):
    from src.app.services import ai_library_analysis as adapter
    from src.shared.ai_models.generation.provider_profiles import AiProviderProfile, DataPolicy, ProfileRegistry

    registry = ProfileRegistry()
    registry.add(AiProviderProfile(
        "remote", "Remote visual", endpoint="https://never-contacted.invalid/v1", model_id="model",
        capabilities=("vision",), modalities=("image", "media_upload"), data_policy=DataPolicy("cloud", True),
    ))
    monkeypatch.setattr(adapter, "_load_profiles", lambda: registry)
    transport = make_transport(env)
    facade = transport._facade
    library_id = env.library.library_id

    def no_scan(*args, **kwargs):
        raise AssertionError("Explicitly unconfigured or unapproved scope must not scan")

    monkeypatch.setattr(facade, "build_ai_library", no_scan)
    try:
        for profile, required, expected in (("missing", True, "模型"), ("remote", False, "授权")):
            with qtbot.waitSignal(transport.ai_library_settings_saved) as saved:
                transport.save_ai_library_settings(library_id, "profiles", (), models={"image": profile},
                                                   kinds=("image",))
            assert saved.args == [""]
            with qtbot.waitSignal(transport.ai_library_index_status,
                                  check_params_cb=lambda state: not state.get("running")) as done:
                transport.start_ai_library_index(library_id)
            state, = done.args
            assert expected in state["error"]
            assert bool(state.get("model_setup_required")) is required
            if required:
                assert state["kind"] == "image"
    finally:
        transport.shutdown()
        stopped = threading.Event()
        facade.when_idle(stopped.set)
        assert stopped.wait(2)


def test_ai_analysis_marks_discard_cancelled_previous_view(qtbot, env, monkeypatch):
    transport = make_transport(env)
    facade = transport._facade
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()

    def read(paths, *, cancel):
        entered.set()
        if release.wait(2) and cancel.requested:
            cancelled.set()
        return set(paths)

    monkeypatch.setattr(facade, "ai_library_analyzed_paths", read)
    received: list[tuple[str, list[str]]] = []
    transport.ai_analysis_marks_loaded.connect(lambda path, marks: received.append((path, marks)))
    try:
        transport.load_ai_analysis_marks("old-view", [str(env.lib / "sample.png")])
        qtbot.waitUntil(entered.is_set)
        transport.load_ai_analysis_marks("new-view", [])
        release.set()
        qtbot.waitUntil(cancelled.is_set)
        stopped = threading.Event()
        facade.when_idle(stopped.set)
        qtbot.waitUntil(stopped.is_set)
        assert received == [("new-view", [])]
    finally:
        release.set()
        transport.shutdown()


def test_scan_without_libraries_fails_with_guidance(qtbot, tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path)
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    catalog.open()
    transport = NativeFilesTransport(lambda: "", None,
                                     facade=FilesFacade(catalog, max_workers=1))
    try:
        with qtbot.waitSignal(transport.scan_failed, timeout=8000) as received:
            transport.start_scan()
        assert "固定" in received.args[0]
    finally:
        transport.shutdown()
        catalog.close()


def test_scan_progress_and_completion(qtbot, env):
    for index in range(250):  # chunk=200：至少两段进度
        _seed(env, f"item-{index:03}.png")
    transport = make_transport(env)
    states = []
    transport.scan_status_loaded.connect(states.append)
    try:
        with qtbot.waitSignal(transport.scan_status_loaded, timeout=8000):
            transport.start_scan()
        qtbot.waitUntil(lambda: states and states[-1].get("running") is False,
                        timeout=15000)
        final = states[-1]
        assert final["phase"] == "完成"
        assert final["scanned"] == 250
        assert final["last_result"]["added"] == 250
        assert any(state.get("running") and state.get("scanned", 0) >= 200
                   for state in states)
    finally:
        transport.shutdown()


def test_scan_cancel_midway(qtbot, env):
    sub = env.lib / "bulk"
    sub.mkdir()
    for index in range(250):
        (sub / f"i{index:03}.png").write_bytes(b"x")
    transport = make_transport(env)
    states = []
    transport.scan_status_loaded.connect(states.append)
    try:
        transport.start_scan()
        qtbot.waitUntil(lambda: any(s.get("running") for s in states), timeout=8000)
        transport.cancel_scan()
        qtbot.waitUntil(lambda: states and states[-1].get("running") is False,
                        timeout=15000)
        assert states[-1]["phase"] == "已取消"
    finally:
        transport.shutdown()


def test_search_requires_pinned_library(qtbot, tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path)
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    catalog.open()
    transport = NativeFilesTransport(lambda: "", None,
                                     facade=FilesFacade(catalog, max_workers=1))
    try:
        with qtbot.waitSignal(transport.search_failed, timeout=8000) as received:
            transport.search_library("anything")
        assert "固定" in received.args[0]
    finally:
        transport.shutdown()
        catalog.close()


def test_search_returns_entries_with_total(qtbot, env):
    for name in ("needle-one.png", "needle-two.png", "other.png"):
        asset = _seed(env, name)
        env.catalog.upsert_asset(env.library.library_id, asset)
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.search_loaded, timeout=8000) as received:
            transport.search_library("needle")
        payload = received.args[0]
        assert payload["total"] == 2
        assert {entry.name for entry in payload["files"]} == {
            "needle-one.png", "needle-two.png"}
        assert payload["truncated"] is False
    finally:
        transport.shutdown()


def test_collections_list_and_members(qtbot, env):
    library_id = env.library.library_id
    cid = env.catalog.create_collection(library_id, "精选")
    first = _seed(env, "m1.png")
    second = _seed(env, "m2.png")
    env.catalog.add_to_collection(cid, str(second))
    env.catalog.add_to_collection(cid, str(first))
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.collections_loaded, timeout=8000) as listed:
            transport.load_collections()
        items = listed.args[0]
        assert items[0]["id"] == cid and items[0]["name"] == "精选"
        assert items[0]["item_count"] == 2

        with qtbot.waitSignal(transport.collection_loaded, timeout=8000) as members:
            transport.load_collection(cid)
        payload = members.args[0]
        assert [entry.path for entry in payload["files"]] == [
            str(second), str(first)]  # 入集顺序
        assert payload["total"] == 2

        # 未知集合：成员为空是如实结果（不冒充错误，也不编造成员）
        with qtbot.waitSignal(transport.collection_loaded, timeout=8000) as unknown:
            transport.load_collection(99999)
        assert unknown.args[0]["files"] == []
    finally:
        transport.shutdown()


# ---------------------------------------------------------------------------
# 预览 / 比较（本地直读）
# ---------------------------------------------------------------------------

def test_preview_local_read_bytes_and_errors(qtbot, env):
    asset = _seed(env, "small.png", b"\x89PNG fake")
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.preview_loaded, timeout=8000) as ok:
            transport.load_preview(str(asset))
        assert ok.args[:2] == [str(asset), b"\x89PNG fake"]
        assert len(ok.args[2]) == 5

        transport.MAX_PREVIEW_BYTES = 8  # 实例级收紧上限：避免真实 64MB 夹具
        big = _seed(env, "big.png", b"x" * 16)
        with qtbot.waitSignal(transport.preview_failed, timeout=8000) as oversize:
            transport.load_preview(str(big))
        assert "过大" in oversize.args[1]
        assert "上限" in oversize.args[1]

        with qtbot.waitSignal(transport.preview_failed, timeout=8000) as missing:
            transport.load_preview(str(env.lib / "gone.png"))
        assert "不存在" in missing.args[1]
    finally:
        transport.shutdown()


def test_preview_same_path_latest_wins(qtbot, env, monkeypatch):
    """[FR-14] 同路径重开：旧读取晚到不得顶替新字节。"""
    from src.features.files.services import facade as facade_module
    asset = _seed(env, "same.png", b"OLD")
    release = threading.Event()
    calls = {"n": 0}
    original = facade_module.read_file_bytes

    def gated(path, max_bytes):
        if calls["n"] == 0:
            calls["n"] += 1
            assert release.wait(5)
        return original(path, max_bytes)

    monkeypatch.setattr(facade_module, "read_file_bytes", gated)
    transport = make_transport(env)
    loaded, failed = [], []
    transport.preview_loaded.connect(lambda p, data, revision: loaded.append((p, data)))
    transport.preview_failed.connect(failed.append)
    try:
        transport.load_preview(str(asset))
        qtbot.waitUntil(lambda: bool(calls["n"]), timeout=8000)
        asset.write_bytes(b"NEW")
        transport.load_preview(str(asset))  # 同路径立刻重开（旧读取在途）
        release.set()
        qtbot.waitUntil(lambda: bool(loaded or failed), timeout=8000)
        qtbot.wait(150)
        assert loaded == [(str(asset), b"NEW")]
        assert failed == []
    finally:
        release.set()
        transport.shutdown()


def test_preview_validates_cached_revisions_off_the_ui_thread(qtbot, env, monkeypatch) -> None:
    from src.features.files.services import facade as facade_module
    from src.features.files.services.thumbnail_cache import GradedThumbnailStamp

    asset = _seed(env, "wait.png", b"OLD")
    entered, release = threading.Event(), threading.Event()
    ui_thread = threading.get_ident()
    revision = GradedThumbnailStamp.revision
    read_bytes = facade_module.read_file_bytes
    reads: list[str] = []

    def gated_revision(path: str) -> tuple[int, int, int, int, int]:
        assert threading.get_ident() != ui_thread
        entered.set()
        assert release.wait(5)
        return revision(path)

    def tracked_read(path: str, limit: int) -> bytes:
        reads.append(path)
        return read_bytes(path, limit)

    monkeypatch.setattr(facade_module, "read_file_bytes", tracked_read)
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.preview_loaded) as first:
            transport.load_preview(str(asset))
        stamp = first.args[2]
        monkeypatch.setattr(GradedThumbnailStamp, "revision", staticmethod(gated_revision))

        with qtbot.waitSignal(transport.preview_loaded) as cached:
            transport.load_preview(str(asset), cached_revisions=(stamp,))
            qtbot.waitUntil(entered.is_set)
            ticked: list[bool] = []
            QTimer.singleShot(0, lambda: ticked.append(True))
            qtbot.waitUntil(lambda: bool(ticked))
            release.set()
        assert cached.args == [str(asset), None, stamp]
        assert reads == [str(asset)]

        replacement = _seed(env, "replacement.png", b"NEW")
        replacement.replace(asset)
        with qtbot.waitSignal(transport.preview_loaded) as replaced:
            transport.load_preview(str(asset), cached_revisions=(stamp,))
        assert replaced.args[:2] == [str(asset), b"NEW"]
        assert replaced.args[2] != stamp
        assert reads == [str(asset), str(asset)]
    finally:
        release.set()
        transport.shutdown()


def test_compare_sessions_isolated_and_cancel_drops(qtbot, env):
    a = _seed(env, "a.png", b"AAA")
    b = _seed(env, "b.png", b"BBB")
    c = _seed(env, "c.png", b"CCC")
    transport = make_transport(env)
    loaded = []
    transport.compare_image_loaded.connect(lambda p, data, s: loaded.append((s, p)))
    try:
        transport.load_compare_images([str(a), str(b)], session_id="s1")
        transport.load_compare_images([str(c), str(a)], session_id="s2")
        qtbot.waitUntil(lambda: len(loaded) == 4, timeout=8000)  # s1×2 + s2×2
        # 两个会话共用同一文件 a.png 也互不作废（独立 key）
        assert {session for session, _ in loaded} == {"s1", "s2"}
        assert ("s2", str(a)) in loaded

        transport.load_compare_images([str(b)], session_id="gone")
        transport.cancel_compare("gone")
        count = len(loaded)
        qtbot.wait(150)
        assert len(loaded) == count  # 关闭会话的迟到结果被丢弃
    finally:
        transport.shutdown()


def test_batch_rename_preview_execute_roundtrip(qtbot, env):
    a = _seed(env, "one.png")
    b = _seed(env, "two.png")
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.batch_rename_preview_ready,
                              timeout=8000) as preview:
            transport.preview_batch_rename(
                [str(a), str(b)], "template", {"template": "{name}_{seq:2}.{ext}"},
                session_id="window-a")
        payload = preview.args[0]
        assert payload["session_id"] == "window-a"
        assert [item["dst_name"] for item in payload["plan"]] == [
            "one_01.png", "two_02.png"]

        with qtbot.waitSignal(transport.batch_rename_finished,
                              timeout=8000) as finished:
            transport.execute_batch_rename(
                [str(a), str(b)], "template", {"template": "{name}_{seq:2}.{ext}"},
                session_id="window-a")
        result = finished.args[0]
        assert result["success"] is True and result["processed"] == 2
        assert (env.lib / "one_01.png").exists()
        assert (env.lib / "two_02.png").exists()

        # 未经预览的会话不得执行（plan_id 缺失）
        with qtbot.waitSignal(transport.batch_rename_finished,
                              timeout=8000) as refused:
            transport.execute_batch_rename(
                [str(env.lib / "one_01.png")], "template",
                {"template": "{name}_x"}, session_id="window-b")
        assert refused.args[0]["success"] is False
        assert "预览" in refused.args[0]["message"]
    finally:
        transport.shutdown()


def test_shutdown_suppresses_late_results(qtbot, env, monkeypatch):
    """关闭后的迟到回调一律静默：用捕获的 listener 直投验证关闭门。"""
    from src.features.files.services.facade import FacadeOutcome
    asset = _seed(env, "late.png", b"LATE")
    facade = FilesFacade(env.catalog, max_workers=1)
    captured: list = []
    monkeypatch.setattr(
        facade, "run",
        lambda *, key, request_id, work, listener: captured.append(listener))
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    loaded = []
    transport.preview_loaded.connect(lambda *args: loaded.append(args))
    try:
        transport.load_preview(str(asset))  # 提交被拦截：listener 未执行
        assert len(captured) == 1
        result = FacadeOutcome(request_id="late", key="preview:x",
                               generation=1, result=(b"LATE", None))
        captured[0](result)  # 关闭前已入 GUI 队列，也必须丢弃。
        transport.shutdown()
        captured[0](result)
        with qtbot.assertNotEmitted(transport.preview_loaded):
            qtbot.wait(100)
        assert loaded == []
    finally:
        transport.shutdown()


# ---------------------------------------------------------------------------
# Local media providers: lifecycle, byte bounds and superseded requests.
# ---------------------------------------------------------------------------

@pytest.fixture
def local_media(env, monkeypatch):
    from src.features.files.services import media
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9V0AAAAASUVORK5CYII=")
    release = threading.Event()
    state = {"calls": [], "active": 0, "peak": 0}
    lock = threading.Lock()

    def decode(path, *, cancel=None, strip=False):
        with lock:
            state["calls"].append((path, strip))
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            if path.startswith("/slow"):
                while not release.wait(0.02):
                    if cancel is not None and cancel.is_set():
                        raise ValueError("cancelled")
            if path == "/bad":
                raise ValueError("no backend")
            if path == "/large":
                return b"x" * (NativeFilesTransport.MAX_THUMBNAIL_BYTES + 1)
            return png
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(media, "thumbnail_bytes", decode)
    transport = make_transport(env)
    # No service URL or credentials may be consulted for any media request.
    transport._server_url = lambda: (_ for _ in ()).throw(AssertionError("retired service accessed"))
    yield transport, state, release, png
    release.set()
    transport.shutdown()


def test_local_thumbnail_version_dedup_and_reload(qtbot, local_media):
    transport, state, _, png = local_media
    path = "/media/+100%20 #素材.png"
    with qtbot.waitSignal(transport.thumbnail_loaded, timeout=3000) as received:
        transport.request_thumbnail(path, 17)
        transport.request_thumbnail(path, 17)
    assert received.args == [path, png]
    assert len(state["calls"]) == 1
    transport.release_thumbnail(path)
    with qtbot.waitSignal(transport.thumbnail_loaded, timeout=3000):
        transport.request_thumbnail(path, 17)
    assert len(state["calls"]) == 2


def test_thumbnail_dispatch_resumes_with_cached_result(qtbot: QtBot, local_media: tuple, env: SimpleNamespace) -> None:
    transport, state, _, png = local_media
    path = str(_seed(env, "cached.jpg"))
    transport.set_thumbnail_dispatch_paused(True)
    transport.request_thumbnail(path, 17)
    assert not transport._thumbnail_replies
    with qtbot.waitSignal(transport.thumbnail_loaded) as first:
        transport.set_thumbnail_dispatch_paused(False)
    assert first.args == [path, png]

    reopened = make_transport(env)
    try:
        with qtbot.waitSignal(reopened.thumbnail_loaded) as second:
            reopened.request_thumbnail(path, 17)
        assert second.args == [path, png]
        assert state["calls"] == [(path, False)]
    finally:
        reopened.shutdown()


def test_local_thumbnail_queue_bounds_cancel_and_shutdown(qtbot, local_media):
    transport, state, release, _ = local_media
    loaded = []
    transport.thumbnail_loaded.connect(lambda *args: loaded.append(args))
    for index in range(200):
        transport.request_thumbnail(f"/slow/{index}")
    assert len(transport._thumbnail_replies) == 4
    assert len(transport._thumbnail_queue) == 128
    qtbot.waitUntil(lambda: bool(state["calls"]), timeout=3000)
    transport.shutdown()
    release.set()
    qtbot.wait(100)
    assert not loaded and not transport._thumbnail_replies and not transport._thumbnail_queue
    assert state["peak"] <= 4


@pytest.mark.parametrize("path", ["/bad", "/large"])
def test_local_thumbnail_errors_are_bounded_and_deduplicated(qtbot, local_media, path):
    transport, state, _, _ = local_media
    with qtbot.waitSignal(transport.thumbnail_failed, timeout=3000) as failed:
        transport.request_thumbnail(path)
    assert failed.args == [path]
    transport.request_thumbnail(path)
    qtbot.wait(30)
    assert len(state["calls"]) == 1


def test_navigation_invalidates_all_inflight_media(qtbot, local_media, env):
    transport, state, release, _ = local_media
    loaded, failed = [], []
    transport.thumbnail_loaded.connect(lambda *args: loaded.append(args))
    transport.thumbnail_failed.connect(failed.append)
    transport.request_thumbnail("/slow/one")
    qtbot.waitUntil(lambda: bool(state["calls"]), timeout=3000)
    with qtbot.waitSignal(transport.loaded, timeout=8000):
        transport.load_directory(str(env.lib))
    release.set()
    qtbot.wait(100)
    assert not loaded and not failed
    with qtbot.waitSignal(transport.thumbnail_loaded, timeout=3000):
        transport.request_thumbnail("/slow/one")


def test_superseded_thumbnail_version_and_inflight_release(qtbot, local_media):
    transport, state, release, _ = local_media
    loaded = []
    transport.thumbnail_loaded.connect(lambda *args: loaded.append(args))
    transport.request_thumbnail("/slow/version", 1)
    qtbot.waitUntil(lambda: bool(state["calls"]), timeout=3000)
    transport.release_thumbnail("/slow/version")
    transport.request_thumbnail("/slow/version", 1)
    transport.request_thumbnail("/slow/version", 2)
    release.set()
    qtbot.waitUntil(lambda: len(loaded) == 1, timeout=3000)
    assert len(state["calls"]) == 2


def test_strip_uses_local_backend_and_is_bounded(qtbot, local_media):
    transport, state, _, png = local_media
    with qtbot.waitSignal(transport.strip_loaded, timeout=3000) as received:
        transport.request_strip("/movie.mov")
    assert received.args == ["/movie.mov", png]
    assert state["calls"] == [("/movie.mov", True)]


# ---------------------------------------------------------------------------
# D2：回收站 + 集合写入
# ---------------------------------------------------------------------------

def _wait_task(qtbot, transport, *args, **kwargs):
    with qtbot.waitSignal(transport.task_finished, timeout=8000) as received:
        transport.run_task(*args, **kwargs)
    return received.args[0]


def test_trash_list_restore_roundtrip(qtbot, env):
    asset = _seed(env, "tossed.png", b"T")
    transport = make_transport(env)
    try:
        job = _wait_task(qtbot, transport, "trash", [str(asset)])
        assert job["successes"] and not asset.exists()

        with qtbot.waitSignal(transport.trash_loaded, timeout=8000) as listed:
            transport.load_trash()
        records = listed.args[0]
        assert len(records) == 1
        record = records[0]
        assert record["item_name"] == "tossed.png"
        assert record["original_path"] == str(asset)

        restored = _wait_task(qtbot, transport, "restore", [str(record["id"])])
        assert restored["successes"] and asset.exists()
        with qtbot.waitSignal(transport.trash_loaded, timeout=8000) as after:
            transport.load_trash()
        assert after.args[0] == []
    finally:
        transport.shutdown()


def test_trash_restore_conflict_then_rename_policy(qtbot, env):
    asset = _seed(env, "clash.png", b"OLD")
    transport = make_transport(env)
    try:
        _wait_task(qtbot, transport, "trash", [str(asset)])
        asset.write_bytes(b"NEW")  # 原路径被新文件占用
        with qtbot.waitSignal(transport.trash_loaded, timeout=8000) as listed:
            transport.load_trash()
        record_id = listed.args[0][0]["id"]

        conflict = _wait_task(qtbot, transport, "restore", [str(record_id)],
                              policy="error")
        assert conflict["failures"] and conflict["failures"][0]["code"] == "DEST_EXISTS"

        renamed = _wait_task(qtbot, transport, "restore", [str(record_id)],
                             policy="rename")
        assert renamed["successes"]
        restored_to = renamed["successes"][0]["to"]
        assert restored_to != str(asset) and "clash" in restored_to
        assert Path(restored_to).exists() and asset.exists()  # 新旧并存
    finally:
        transport.shutdown()


def test_trash_purge_removes_permanently(qtbot, env):
    asset = _seed(env, "gone.png", b"G")
    transport = make_transport(env)
    try:
        _wait_task(qtbot, transport, "trash", [str(asset)])
        with qtbot.waitSignal(transport.trash_loaded, timeout=8000) as listed:
            transport.load_trash()
        record_id = listed.args[0][0]["id"]

        purged = _wait_task(qtbot, transport, "purge", [str(record_id)])
        assert purged["successes"]
        with qtbot.waitSignal(transport.trash_loaded, timeout=8000) as after:
            transport.load_trash()
        assert after.args[0] == []
        # 彻底删除后：文件本体与 trash 目录副本都不在
        assert not asset.exists()
        trash_dirs = list((env.root / "files-data" / "trash").glob("*/*"))
        assert all("gone.png" not in item.name for item in trash_dirs)
    finally:
        transport.shutdown()


def test_collection_write_roundtrip(qtbot, env):
    a = _seed(env, "c1.png")
    b = _seed(env, "c2.png")
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.collection_changed, timeout=8000) as created:
            transport.create_collection("精选", [str(a), str(b)])
        assert created.args[0] is True
        with qtbot.waitSignal(transport.collections_loaded, timeout=8000) as listed:
            transport.load_collections()
        item = listed.args[0][0]
        assert item["name"] == "精选" and item["item_count"] == 2
        cid = item["id"]

        with qtbot.waitSignal(transport.collection_changed, timeout=8000) as renamed:
            transport.rename_collection(cid, "重命名后")
        assert renamed.args[0] is True

        with qtbot.waitSignal(transport.collection_loaded, timeout=8000) as members:
            transport.load_collection(cid)
        assert [entry.path for entry in members.args[0]["files"]] == [str(a), str(b)]

        with qtbot.waitSignal(transport.collection_changed, timeout=8000) as removed:
            transport.remove_collection_members(cid, [str(a)])
        assert removed.args[0] is True
        with qtbot.waitSignal(transport.collection_loaded, timeout=8000) as after:
            transport.load_collection(cid)
        assert [entry.path for entry in after.args[0]["files"]] == [str(b)]

        with qtbot.waitSignal(transport.collection_changed, timeout=8000) as added:
            transport.add_collection_members(cid, [str(a)])
        assert added.args[0] is True

        with qtbot.waitSignal(transport.collection_changed, timeout=8000) as deleted:
            transport.delete_collection(cid)
        assert deleted.args[0] is True
        with qtbot.waitSignal(transport.collections_loaded, timeout=8000) as empty:
            transport.load_collections()
        assert empty.args[0] == []

        # 空名/未选择：同步拒绝并说明原因
        with qtbot.waitSignal(transport.collection_changed, timeout=8000) as refused:
            transport.create_collection("   ", [str(a)])
        assert refused.args[0] is False
    finally:
        transport.shutdown()


def test_local_inspector_metadata_and_source_dimensions_roundtrip(qtbot, env, monkeypatch):
    from PyQt6.QtCore import QItemSelectionModel, QSettings, Qt
    from PyQt6.QtGui import QColor, QImage

    from src.app.ui.native_files_panel import NativeFilesPanel
    from src.app.ui.native_files_prefs import NativeFilesPreferences

    local = env.root / "local"
    local.mkdir()
    asset = local / "wide.png"
    image = QImage(960, 400, QImage.Format.Format_RGB32)
    image.fill(QColor("#29515a"))
    assert image.save(str(asset))
    preference_file = env.root / "prefs.ini"
    prefs = NativeFilesPreferences(QSettings(str(preference_file), QSettings.Format.IniFormat))
    transport = make_transport(env)
    panel = NativeFilesPanel(preferences=prefs, transport=transport)
    monkeypatch.setattr(panel, "is_browsable_path", lambda path: path in (str(local), str(asset)))
    qtbot.addWidget(panel)
    panel.resize(1020, 600)
    panel.show()
    transport.loaded.connect(panel.set_entries)
    transport.metadata_snapshot.connect(panel.apply_metadata_snapshot)
    transport.metadata_applied.connect(panel.apply_metadata_result)
    panel.metadata_patch_requested.connect(transport.patch_metadata)
    try:
        with qtbot.waitSignal(transport.loaded):
            transport.load_directory(str(local))
        panel.inspector_button.setChecked(True)
        panel.selection.setCurrentIndex(panel.proxy.index(0, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect
                                        | QItemSelectionModel.SelectionFlag.Rows)
        with qtbot.waitSignal(transport.metadata_applied):
            qtbot.mouseClick(panel.color_buttons[3], Qt.MouseButton.LeftButton)
        panel.comment_edit.setFocus()
        qtbot.keyClicks(panel.comment_edit, "Keep for export")
        with qtbot.waitSignal(transport.metadata_applied):
            qtbot.keyClick(panel.comment_edit, Qt.Key.Key_Return)
        panel.tag_edit.setFocus()
        qtbot.keyClicks(panel.tag_edit, "Portrait, Travel")
        with qtbot.waitSignal(transport.metadata_applied):
            qtbot.keyClick(panel.tag_edit, Qt.Key.Key_Return)
        with qtbot.waitSignal(transport.loaded):
            transport.load_directory(str(local))
        entry = panel.model.entries[0]
        assert (entry.user.color, entry.user.comments, entry.user.tags) == (3, "Keep for export", ("Portrait", "Travel"))
        panel._set_card_metadata(frozenset({"resolution", "size", "tags"}))
        qtbot.waitUntil(lambda: panel.card_delegate.metadata_lines(entry)[0] == "960×400")
        rect = panel.icon_view.visualRect(panel.proxy.index(0, 0))
        assert panel.card_delegate.name_rect(rect).height() == 20
        assert panel.card_delegate.metadata_lines(entry) == ["960×400", entry.size_label, entry.user.tag_text]
        assert prefs.card_metadata == ["resolution", "size", "tags"]

        lib = env.catalog.register_library(local)
        env.catalog.upsert_asset(lib.library_id, asset, width=960, height=400)
        matches = FilesQueryService(env.catalog).search("Keep for export", library_id=lib.library_id)
        assert len(matches.entries) == 1
        assert matches.entries[0].user == entry.user
        assert matches.entries[0].technical.resolution == "960×400"
        assert env.catalog.asset("", asset) is None
    finally:
        panel.shutdown()
        transport.shutdown()


def test_sidebar_counts_direct_visible_files_and_refreshes_aliases(qtbot, env):
    from src.app.ui.native_files_locations import HomeDirectoryTree

    transport = make_transport(env)
    tree = HomeDirectoryTree(str(env.root), lambda entry: True)
    qtbot.addWidget(tree)
    empty = env.lib / "Empty"
    empty.mkdir()
    (env.lib / ".hidden").touch()
    visible_file = env.lib / "notes.docx"
    visible_file.touch()
    home_folder = {"name": env.lib.name, "path": str(env.lib)}
    tree.enable_grade_sets()
    tree.set_grade_sets(((1, "Portraits", 12),))
    tree.set_libraries([{"library_id": env.library.library_id, "name": "Library alias", "path": str(env.lib)}])
    tree.set_children(str(env.root), [home_folder])
    tree.set_children(str(env.lib), [{"name": empty.name, "path": str(empty)}])
    tree.setExpanded(tree.home_item.index(), True)
    tree.setExpanded(tree.home_item.child(0).index(), True)
    tree.folder_counts_requested.connect(transport.load_folder_counts)
    transport.folder_counts_loaded.connect(tree.apply_folder_counts)
    tree.resize(260, 420)
    tree.show()
    try:
        home_item = tree.home_item.child(0)
        alias = tree._library_roots[env.library.library_id]
        grade_set = tree._grade_set_group.child(0)
        assert grade_set.text() == "Portraits"
        assert grade_set.data(tree.COUNT_ROLE) == 12
        qtbot.waitUntil(lambda: home_item.data(tree.COUNT_ROLE) == 1
                        and alias.data(tree.COUNT_ROLE) == 1
                        and home_item.child(0).data(tree.COUNT_ROLE) == 0)
        assert home_item.text() == env.lib.name
        assert alias.text() == "Library alias"
        visible_file.unlink()
        tree.refresh(str(env.lib))
        qtbot.waitUntil(lambda: home_item.data(tree.COUNT_ROLE) == 0 and alias.data(tree.COUNT_ROLE) == 0)
        assert FilesQueryService(env.catalog).directory_file_counts([str(env.lib / "missing")]) == {
            str(env.lib / "missing"): None,
        }
    finally:
        transport.shutdown()


def test_library_transfer_publishes_imported_library(qtbot: QtBot, env) -> None:
    _seed(env, "photo.jpg")
    package = env.root / "library.toposlibrary"
    receiver = env.root / "receiver"
    receiver.mkdir()
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.library_transfer_finished, timeout=5000) as exported:
            transport.export_library(env.library.library_id, str(package))
        assert exported.args[0] is True
        assert package.is_file()

        with qtbot.waitSignals(
            [transport.library_transfer_finished, transport.libraries_loaded], timeout=5000
        ) as imported:
            transport.import_library(str(package), str(receiver))
        assert imported.all_signals_and_args[0].args[0] is True
        assert any(item["path"] == str(receiver / env.library.name)
                   for item in imported.all_signals_and_args[1].args[0])
    finally:
        transport.shutdown()
