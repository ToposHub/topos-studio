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
            query = parse_qs(urlsplit(self.path).query)
            path = query["path"][0]
            calls.append((path, self.headers.get("X-Auth-Token")))
            if path == "/slow":
                release.wait(3)
            status = 423 if path == "/locked" else 200
            payload = {"files": [{"name": "file", "path": path + "/file", "type": "video"}]}
            if path == "/locked":
                payload = {"locked": True, "error": "locked"}
            if path == "/invalid":
                payload = {"files": [{"name": "file"}]}
            if path == "/missing":
                payload = {"files": [], "error": "Path not found"}
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
