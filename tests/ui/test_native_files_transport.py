"""Exercise Qt HTTP transport against an isolated local protocol fixture."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from src.app.ui.native_files_transport import NativeFilesTransport


@pytest.fixture
def service():
    calls = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            path = query["path"][0]
            calls.append((path, self.headers.get("X-Auth-Token")))
            if path == "/slow" or path.startswith("/slow-tree/"):
                release.wait(3)
            status = 423 if path == "/locked" else 200
            payload = {"files": [{"name": "file", "path": path + "/file", "type": "video"}]}
            if path == "/locked":
                payload = {"locked": True, "error": "locked"}
            if path == "/invalid":
                payload = {"files": [{"name": "file"}]}
            if path == "/missing":
                payload = {"files": [], "error": "Path not found"}
            if parsed.path == "/api/tree":
                payload = [{"name": "folder", "path": path + "/folder"}]
                if path == "/invalid":
                    payload = [{"name": "missing path"}]
                elif path == "/invalid-root":
                    payload = {"error": "invalid tree"}
                elif path == "/empty-tree":
                    payload = []
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(json.dumps(payload).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass  # A cancelled client is expected in the stale reply test.

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}", calls, release
    release.set()
    server.shutdown()
    server.server_close()
    worker.join(2)


def test_exact_path_and_token_survive_http_roundtrip(qtbot, service):
    base, calls, _ = service
    transport = NativeFilesTransport(lambda: base, "local-secret")
    path = "/tmp/ 字面+100%20 #素材'\n"
    try:
        with qtbot.waitSignal(transport.loaded, timeout=3000) as received:
            transport.load_directory(path)
        assert calls == [(path, "local-secret")]
        assert received.args[0] == path
        assert received.args[1][0]["path"] == path + "/file"
    finally:
        transport.shutdown()


@pytest.mark.parametrize("path", ["/locked", "/invalid", "/missing"])
def test_failed_directory_never_publishes_entries(qtbot, service, path):
    base, _, _ = service
    transport = NativeFilesTransport(lambda: base, "token")
    loaded = []
    transport.loaded.connect(lambda *args: loaded.append(args))
    with qtbot.waitSignal(transport.failed, timeout=3000) as received:
        transport.load_directory(path)
    assert received.args[0] == path
    assert received.args[1]
    assert loaded == []
    transport.shutdown()


def test_late_cancelled_request_does_not_replace_new_directory(qtbot, service):
    base, calls, release = service
    transport = NativeFilesTransport(lambda: base, "token")
    loaded = []
    transport.loaded.connect(lambda path, _: loaded.append(path))
    transport.load_directory("/slow")
    qtbot.waitUntil(lambda: bool(calls), timeout=3000)
    with qtbot.waitSignal(transport.loaded, timeout=3000):
        transport.load_directory("/new")
    release.set()
    qtbot.wait(100)
    assert loaded == ["/new"]
    transport.shutdown()


def test_non_local_service_is_rejected_without_sending_credentials(qtbot):
    transport = NativeFilesTransport(lambda: "http://example.com", "secret")
    with qtbot.waitSignal(transport.failed, timeout=100):
        transport.load_directory("/media")
    assert transport._reply is None


def test_tree_path_auth_duplicate_and_reload(qtbot, service):
    base, calls, _ = service
    transport = NativeFilesTransport(lambda: base, "tree-secret")
    path = "/tmp/字面+100%20 #树"
    with qtbot.waitSignal(transport.tree_loaded, timeout=3000) as received:
        transport.load_tree(path)
        transport.load_tree(path)
    assert received.args == [path, [{"name": "folder", "path": path + "/folder"}]]
    assert calls == [(path, "tree-secret")]
    with qtbot.waitSignal(transport.tree_loaded, timeout=3000):
        transport.load_tree(path)
    assert len(calls) == 2
    transport.shutdown()


@pytest.mark.parametrize("path", ["/invalid", "/invalid-root", "/locked"])
def test_tree_rejects_invalid_entries_or_http_failure(qtbot, service, path):
    base, _, _ = service
    transport = NativeFilesTransport(lambda: base, "secret")
    loaded = []
    transport.tree_loaded.connect(lambda *args: loaded.append(args))
    with qtbot.waitSignal(transport.tree_failed, timeout=3000) as received:
        transport.load_tree(path)
    assert received.args[0] == path
    assert received.args[1]
    assert not loaded
    assert not transport._tree_pending_paths
    transport.shutdown()


def test_empty_tree_is_success(qtbot, service):
    base, _, _ = service
    transport = NativeFilesTransport(lambda: base, "secret")
    with qtbot.waitSignal(transport.tree_loaded, timeout=3000) as received:
        transport.load_tree("/empty-tree")
    assert received.args == ["/empty-tree", []]
    transport.shutdown()


def test_content_navigation_does_not_cancel_expanded_tree(qtbot, service):
    base, calls, release = service
    transport = NativeFilesTransport(lambda: base, "secret")
    transport.load_tree("/slow")
    qtbot.waitUntil(lambda: bool(calls), timeout=3000)
    with qtbot.waitSignal(transport.loaded, timeout=3000):
        transport.load_directory("/new")
    transport.cancel()
    with qtbot.waitSignal(transport.tree_loaded, timeout=3000) as received:
        release.set()
    assert received.args[0] == "/slow"
    transport.shutdown()


def test_tree_queue_bounds_and_shutdown_suppress_late_results(qtbot, service):
    base, calls, release = service
    transport = NativeFilesTransport(lambda: base, "secret")
    loaded, failed = [], []
    transport.tree_loaded.connect(lambda *args: loaded.append(args))
    transport.tree_failed.connect(lambda *args: failed.append(args))
    for index in range(200):
        transport.load_tree(f"/slow-tree/{index}")
    assert len(transport._tree_replies) == 4
    assert len(transport._tree_queue) == 128
    assert len(failed) == 68
    qtbot.waitUntil(lambda: len(calls) == 4, timeout=3000)
    transport.shutdown()
    assert not transport._tree_replies
    assert not transport._tree_queue
    assert not transport._tree_pending_paths
    release.set()
    qtbot.wait(100)
    assert not loaded
    assert len(failed) == 68
    transport.load_tree("/after-close")
    assert len(calls) == 4


def test_tree_nonlocal_runtime_is_rejected(qtbot):
    transport = NativeFilesTransport(lambda: "http://example.com", "secret")
    with qtbot.waitSignal(transport.tree_failed, timeout=100):
        transport.load_tree("/media")
    assert not transport._tree_replies
    assert not transport._tree_pending_paths
    transport.shutdown()


@pytest.fixture
def thumbnail_service():
    import base64

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


def test_directory_navigation_cancels_inflight_and_queued_thumbnails(qtbot, thumbnail_service):
    base, state, release, _ = thumbnail_service
    transport = NativeFilesTransport(lambda: base, "secret")
    loaded, failed = [], []
    transport.thumbnail_loaded.connect(lambda path, _: loaded.append(path))
    transport.thumbnail_failed.connect(failed.append)
    for index in range(8):
        transport.request_thumbnail(f"/slow/{index}")
    qtbot.waitUntil(lambda: len(state["calls"]) == 4, timeout=3000)
    with qtbot.waitSignal(transport.loaded, timeout=3000):
        transport.load_directory("/new")
    assert not transport._thumbnail_replies
    assert not transport._thumbnail_queue
    release.set()
    qtbot.wait(100)
    assert loaded == []
    assert failed == []
    assert len(state["calls"]) == 5
    with qtbot.waitSignal(transport.thumbnail_loaded, timeout=3000):
        transport.request_thumbnail("/slow/0")  # New generation permits a fresh attempt.
    transport.shutdown()


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


@pytest.fixture
def metadata_service():
    """U05/B01/U03 的端到端 HTTP 替身：POST 写入 + capabilities + 原始字节预览。"""
    png = b"\x89PNG\r\n\x1a\nfake-bytes"
    release = threading.Event()
    state = {"posts": [], "gets": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            parsed = urlsplit(self.path)
            state["posts"].append((parsed.path, payload, self.headers.get("X-Auth-Token")))
            if parsed.path == "/api/update_meta" and payload.get("path") == "/deny":
                body, status = {"success": False, "message": "只读挂载"}, 403
            elif parsed.path == "/api/batch/meta" and "/deny" in payload.get("files", []):
                body, status = {"success": False, "message": "部分文件锁住"}, 409
            else:
                body, status = {"success": True}, 200
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            path = query.get("path", [""])[0]
            state["gets"].append((parsed.path, path, self.headers.get("X-Auth-Token")))
            if parsed.path == "/api/capabilities":
                raw = json.dumps({"version": 1, "capabilities": [
                    {"key": "metadata.patch", "available": True, "reason": ""},
                    {"key": "preview.video", "available": False, "reason": "ffmpeg 缺失"},
                ]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if parsed.path == "/api/file":
                if path == "/slow":
                    release.wait(3)
                if path == "/locked":
                    body, status = b'{"locked": true}', 423
                elif path == "/empty":
                    body, status = b"", 204
                elif path == "/huge":
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(NativeFilesTransport.MAX_PREVIEW_BYTES + 10))
                    self.end_headers()
                    return
                else:
                    body, status = png, 200
                self.send_response(status)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            raw = b'{"files": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

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


def test_capabilities_are_loaded_with_token(qtbot, metadata_service):
    base, state, _, _ = metadata_service
    transport = NativeFilesTransport(lambda: base, "secret")
    with qtbot.waitSignal(transport.capabilities_loaded, timeout=3000) as received:
        transport.load_capabilities()
    assert state["gets"][-1] == ("/api/capabilities", "", "secret")
    keys = {item["key"]: item for item in received.args[0]}
    assert keys["preview.video"]["available"] is False
    assert keys["preview.video"]["reason"]
    transport.shutdown()


def test_single_file_patch_uses_update_meta(qtbot, metadata_service):
    base, state, _, _ = metadata_service
    transport = NativeFilesTransport(lambda: base, "secret")
    with qtbot.waitSignal(transport.metadata_applied, timeout=3000) as received:
        transport.patch_metadata(["/a.mov"], {"rating": 4})
    assert state["posts"][0][0] == "/api/update_meta"
    assert state["posts"][0][1] == {"path": "/a.mov", "rating": 4}
    assert received.args[0] == ["/a.mov"]
    assert received.args[1] == {"rating": 4}
    transport.shutdown()


def test_multi_file_patch_is_one_batch_request_with_comments(qtbot, metadata_service):
    base, state, _, _ = metadata_service
    transport = NativeFilesTransport(lambda: base, "secret")
    with qtbot.waitSignal(transport.metadata_applied, timeout=3000) as received:
        transport.patch_metadata(["/a.mov", "/b.mov"], {"comments": "统一", "color": 0})
    assert [item[0] for item in state["posts"]] == ["/api/batch/meta"]
    assert state["posts"][0][1] == {"files": ["/a.mov", "/b.mov"], "comments": "统一", "color": 0}
    assert received.args[0] == ["/a.mov", "/b.mov"]
    transport.shutdown()


def test_patch_failure_surfaces_server_message_and_publishes_nothing(qtbot, metadata_service):
    base, state, _, _ = metadata_service
    transport = NativeFilesTransport(lambda: base, "secret")
    with qtbot.waitSignal(transport.metadata_failed, timeout=3000) as received:
        transport.patch_metadata(["/deny", "/b.mov"], {"rating": 5})
    assert "部分文件锁住" in received.args[2]
    applied: list[tuple] = []
    transport.metadata_applied.connect(lambda *args: applied.append(args))
    transport.patch_metadata(["/deny"], {"tags": "x"})
    qtbot.waitUntil(lambda: len(state["posts"]) == 2, timeout=3000)
    qtbot.wait(150)  # 给失败回包一个汇合窗口，确认没有"成功"误报
    assert applied == []
    transport.shutdown()


def test_preview_bytes_and_locked_and_empty_states(qtbot, metadata_service):
    base, _, _, png = metadata_service
    transport = NativeFilesTransport(lambda: base, "secret")
    with qtbot.waitSignal(transport.preview_loaded, timeout=3000) as received:
        transport.load_preview("/a.png")
    assert received.args == ["/a.png", png]
    with qtbot.waitSignal(transport.preview_failed, timeout=3000) as locked:
        transport.load_preview("/locked")
    assert locked.args[0] == "/locked"
    assert "锁定" in locked.args[1]
    with qtbot.waitSignal(transport.preview_failed, timeout=3000) as empty:
        transport.load_preview("/empty")
    assert "没有可预览内容" in empty.args[1]
    transport.shutdown()


def test_preview_rejects_oversized_without_decompressing_everything(qtbot, metadata_service):
    base, _, _, _ = metadata_service
    transport = NativeFilesTransport(lambda: base, "secret")
    with qtbot.waitSignal(transport.preview_failed, timeout=3000) as received:
        transport.load_preview("/huge")
    assert "过大" in received.args[1]
    transport.shutdown()


def test_cancelled_preview_never_publishes_stale_bytes(qtbot, metadata_service):
    base, state, release, _ = metadata_service
    transport = NativeFilesTransport(lambda: base, "secret")
    transport.load_preview("/slow")
    qtbot.waitUntil(lambda: len(state["gets"]) == 1, timeout=3000)
    transport.cancel_preview()
    release.set()
    with qtbot.assertNotEmitted(transport.preview_loaded):
        qtbot.wait(200)
    transport.shutdown()
