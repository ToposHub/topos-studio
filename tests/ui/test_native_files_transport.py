"""Transport 层测试：D1 换轨后数据面走 facade/本地直读，媒体桥保留 HTTP。

facade 部分：注入 tmp 配置根的真实 catalog/facade（合成素材），不启动
任何 HTTP 服务——数据路由对旧服务启停无感是本批的验收点。缩略图桥
（等 E 阶段 provider 接管）仍按本地 HTTP 协议夹具验证。
"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

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

        # 未固定库的路径：逐条失败并说明原因，不假装成功
        outsider = env.root / "outside.png"
        outsider.write_bytes(b"x")
        with qtbot.waitSignal(transport.metadata_failed, timeout=8000) as received:
            transport.patch_metadata([str(outsider)], {"rating": 3})
        assert "固定" in received.args[2]
        assert received.args[0] == [str(outsider)]
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
    finally:
        transport.shutdown()


# ---------------------------------------------------------------------------
# 常用位置 / 索引 / 搜索 / 集合
# ---------------------------------------------------------------------------

def test_libraries_pin_unpin_roundtrip(qtbot, env):
    transport = make_transport(env)
    try:
        with qtbot.waitSignal(transport.library_changed, timeout=8000) as changed:
            transport.pin_library(str(env.lib))
        assert changed.args == [True, "已固定为常用位置"]
        with qtbot.waitSignal(transport.libraries_loaded, timeout=8000) as listed:
            transport.load_libraries()
        assert [item["path"] for item in listed.args[0]] == [str(env.lib)]
        assert listed.args[0][0]["library_id"] == env.library.library_id

        with qtbot.waitSignal(transport.library_changed, timeout=8000) as unpinned:
            transport.unpin_library(str(env.lib))
        assert unpinned.args[0] is True
        with qtbot.waitSignal(transport.libraries_loaded, timeout=8000) as after:
            transport.load_libraries()
        assert after.args[0] == []

        with qtbot.waitSignal(transport.library_changed, timeout=8000) as unknown:
            transport.unpin_library(str(env.lib))
        assert unknown.args[0] is False
        assert "不在常用位置" in unknown.args[1]
    finally:
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
        assert ok.args == [str(asset), b"\x89PNG fake"]

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
    transport.preview_loaded.connect(lambda p, data: loaded.append((p, data)))
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
        facade, "read_file",
        lambda path, *, request_id, listener, max_bytes: captured.append(listener))
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    loaded = []
    transport.preview_loaded.connect(lambda *args: loaded.append(args))
    try:
        transport.load_preview(str(asset))  # 提交被拦截：listener 未执行
        assert len(captured) == 1
        transport.shutdown()
        captured[0](FacadeOutcome(request_id="late", key="read:x",
                                  generation=1, result=b"LATE"))
        with qtbot.assertNotEmitted(transport.preview_loaded):
            qtbot.wait(100)
        assert loaded == []
    finally:
        transport.shutdown()


# ---------------------------------------------------------------------------
# HTTP 媒体桥：缩略图（等 E 阶段 provider 接管前保留原协议）
# ---------------------------------------------------------------------------

@pytest.fixture
def thumbnail_service():
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9V0AAAAASUVORK5CYII=")
    release = threading.Event()
    state = {"calls": [], "active": 0, "peak": 0, "ready": False}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            path = query.get("path", [""])[0]
            with lock:
                state["calls"].append((parsed.path, query, self.headers.get("X-Auth-Token")))
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                if path.startswith("/slow"):
                    release.wait(3)
                if parsed.path == "/api/list":
                    body, mime, status = b'{"files": []}', "application/json", 200
                elif path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/leak")
                    self.end_headers()
                    return
                elif path == "/empty":
                    body, mime, status = b"", "image/png", 204
                elif path == "/pending" or (path == "/cold-image" and not state["ready"]):
                    body, mime, status = b"", "image/png", 202
                elif path == "/invalid":
                    body, mime, status = b'{"error":"bad"}', "application/json", 200
                elif path.startswith("/large"):
                    body, mime, status = b"x" * (NativeFilesTransport.MAX_THUMBNAIL_BYTES + 1), "image/png", 200
                else:
                    body, mime, status = png, "image/png", 200
                self.send_response(status)
                self.send_header("Content-Type", mime)
                if path != "/large-stream":
                    self.send_header("Content-Length", str(len(body)))
                if path == "/placeholder":
                    self.send_header("Content-Disposition", "inline; filename=logo.webp")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    state["active"] -= 1

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}", state, release, png
    release.set()
    server.shutdown()
    server.server_close()
    worker.join(2)


def test_thumbnail_path_version_auth_and_duplicate_requests(qtbot, thumbnail_service):
    base, state, _, png = thumbnail_service
    transport = NativeFilesTransport(lambda: base, "secret")
    path = "/media/+100%20 #素材.png"
    with qtbot.waitSignal(transport.thumbnail_loaded, timeout=3000) as received:
        transport.request_thumbnail(path, 17)
        transport.request_thumbnail(path, 17)
    assert received.args == [path, png]
    assert state["calls"] == [("/api/thumb", {"path": [path], "size": ["small"], "v": ["17"]}, "secret")]
    transport.request_thumbnail(path, 17)
    qtbot.wait(30)
    assert len(state["calls"]) == 1
    with qtbot.waitSignal(transport.thumbnail_loaded, timeout=3000):
        transport.request_thumbnail(path, 18)
    assert len(state["calls"]) == 2
    transport.shutdown()


def test_thumbnail_concurrency_queue_bounds_and_shutdown(qtbot, thumbnail_service):
    base, state, release, _ = thumbnail_service
    transport = NativeFilesTransport(lambda: base, "secret")
    loaded = []
    transport.thumbnail_loaded.connect(lambda path, _: loaded.append(path))
    for index in range(200):
        transport.request_thumbnail(f"/slow/{index}")
    assert len(transport._thumbnail_replies) == 4
    assert len(transport._thumbnail_queue) == 128
    qtbot.waitUntil(lambda: len(state["calls"]) == 4, timeout=3000)
    assert state["peak"] <= 4
    transport.shutdown()
    assert not transport._thumbnail_replies
    assert not transport._thumbnail_queue
    release.set()
    qtbot.wait(100)
    transport.request_thumbnail("/after-shutdown")
    assert loaded == []
    assert len(state["calls"]) == 4


@pytest.mark.parametrize("path", ["/empty", "/invalid", "/large", "/large-stream", "/redirect"])
def test_failed_thumbnail_is_bounded_and_not_retried(qtbot, thumbnail_service, path):
    base, state, _, _ = thumbnail_service
    transport = NativeFilesTransport(lambda: base, "secret")
    loaded = []
    transport.thumbnail_loaded.connect(lambda *args: loaded.append(args))
    with qtbot.waitSignal(transport.thumbnail_failed, timeout=3000) as received:
        transport.request_thumbnail(path)
    assert received.args == [path]
    transport.request_thumbnail(path)
    qtbot.wait(30)
    assert len(state["calls"]) == 1
    assert loaded == []
    assert not transport._thumbnail_replies
    transport.shutdown()


@pytest.mark.parametrize("path", ["/pending", "/placeholder"])
def test_pending_thumbnail_is_distinct_from_failure(qtbot, thumbnail_service, path):
    base, state, _, _ = thumbnail_service
    transport = NativeFilesTransport(lambda: base, "secret")
    loaded, failed = [], []
    transport.thumbnail_loaded.connect(lambda *args: loaded.append(args))
    transport.thumbnail_failed.connect(failed.append)
    with qtbot.waitSignal(transport.thumbnail_pending, timeout=3000) as received:
        transport.request_thumbnail(path)
    assert received.args == [path]
    transport.request_thumbnail(path)
    qtbot.wait(30)
    assert len(state["calls"]) == 1  # Retry belongs to the view's bounded schedule.
    assert not loaded and not failed
    transport.shutdown()


def test_cold_thumbnail_can_be_released_and_loaded_after_generation(qtbot, thumbnail_service):
    base, state, _, png = thumbnail_service
    transport = NativeFilesTransport(lambda: base, "secret")
    with qtbot.waitSignal(transport.thumbnail_pending, timeout=3000):
        transport.request_thumbnail("/cold-image", 5)
    state["ready"] = True
    transport.release_thumbnail("/cold-image")
    with qtbot.waitSignal(transport.thumbnail_loaded, timeout=3000) as received:
        transport.request_thumbnail("/cold-image", 5)
    assert received.args == ["/cold-image", png]
    assert len(state["calls"]) == 2
    transport.shutdown()


def test_directory_navigation_cancels_inflight_and_queued_thumbnails(
        qtbot, thumbnail_service, tmp_path):
    """数据面换轨后，目录导航仍作废在途/排队缩略图（cancel 语义保留）。"""
    base, state, release, _ = thumbnail_service
    target = tmp_path / "new"
    target.mkdir()
    locator = FilesDataLocator.for_config_root(tmp_path / "data")
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    catalog.open()
    transport = NativeFilesTransport(
        lambda: base, "secret", facade=FilesFacade(catalog, max_workers=1))
    loaded, failed = [], []
    transport.thumbnail_loaded.connect(lambda path, _: loaded.append(path))
    transport.thumbnail_failed.connect(failed.append)
    try:
        for index in range(8):
            transport.request_thumbnail(f"/slow/{index}")
        qtbot.waitUntil(lambda: len(state["calls"]) >= 4, timeout=3000)
        with qtbot.waitSignal(transport.loaded, timeout=8000):
            transport.load_directory(str(target))  # 本地直读，无需 HTTP
        assert not transport._thumbnail_replies
        assert not transport._thumbnail_queue
        release.set()
        qtbot.wait(100)
        assert loaded == []
        assert failed == []
        calls_before = len(state["calls"])
        with qtbot.waitSignal(transport.thumbnail_loaded, timeout=3000):
            transport.request_thumbnail("/slow/0")  # New generation permits a fresh attempt.
        assert len(state["calls"]) > calls_before
    finally:
        transport.shutdown()
        catalog.close()


def test_thumbnail_nonlocal_runtime_is_rejected(qtbot):
    transport = NativeFilesTransport(lambda: "http://example.com", "secret")
    with qtbot.waitSignal(transport.thumbnail_failed, timeout=100):
        transport.request_thumbnail("/media/image.png")
    assert not transport._thumbnail_replies
    transport.shutdown()


def test_thumbnail_queue_drains_and_superseded_version_is_not_published(qtbot, thumbnail_service):
    base, state, release, _ = thumbnail_service
    transport = NativeFilesTransport(lambda: base, "secret")
    loaded = []
    transport.thumbnail_loaded.connect(lambda path, _: loaded.append(path))
    transport.request_thumbnail("/slow/version", 1)
    qtbot.waitUntil(lambda: len(state["calls"]) == 1, timeout=3000)
    transport.request_thumbnail("/slow/version", 2)
    for index in range(10):
        transport.request_thumbnail(f"/image/{index}")
    release.set()
    qtbot.waitUntil(lambda: len(loaded) == 11, timeout=3000)
    assert loaded.count("/slow/version") == 1
    assert len(state["calls"]) == 12
    assert state["peak"] <= 4
    assert not transport._thumbnail_replies
    assert not transport._thumbnail_queue
    transport.shutdown()


def test_released_completed_thumbnail_can_reload_same_version(qtbot, thumbnail_service):
    base, state, _, png = thumbnail_service
    transport = NativeFilesTransport(lambda: base, "secret")
    with qtbot.waitSignal(transport.thumbnail_loaded, timeout=3000):
        transport.request_thumbnail("/image", 7)
    transport.release_thumbnail("/image")
    with qtbot.waitSignal(transport.thumbnail_loaded, timeout=3000) as received:
        transport.request_thumbnail("/image", 7)
    assert received.args == ["/image", png]
    assert len(state["calls"]) == 2
    transport.shutdown()


def test_releasing_inflight_thumbnail_keeps_network_deduplication(qtbot, thumbnail_service):
    base, state, release, _ = thumbnail_service
    transport = NativeFilesTransport(lambda: base, "secret")
    transport.request_thumbnail("/slow/image", 7)
    qtbot.waitUntil(lambda: len(state["calls"]) == 1, timeout=3000)
    transport.release_thumbnail("/slow/image")
    transport.request_thumbnail("/slow/image", 7)
    with qtbot.waitSignal(transport.thumbnail_loaded, timeout=3000):
        release.set()
    assert len(state["calls"]) == 1
    transport.shutdown()
