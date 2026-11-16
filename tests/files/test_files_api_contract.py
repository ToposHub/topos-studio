"""B01/B02/B03：/api/list 与 /api/capabilities 的契约测试。

验证：技术字段（width/height/codec/missing）随列表下发、total/truncated/
request_id 显式表达、能力清单可被 UI 直接消费。不访问真实用户数据库：
``routes._db`` 被替换为内存表。
"""
from __future__ import annotations

import sqlite3
import sys
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILE_EXPLORER = ROOT / "external" / "file_explorer"
if str(FILE_EXPLORER) not in sys.path:
    sys.path.insert(0, str(FILE_EXPLORER))

pytest.importorskip("PyQt6.QtWebEngineCore")

import main as fe_main  # noqa: E402
import routes as fe_routes  # noqa: E402
import const  # noqa: E402


@pytest.fixture()
def client():
    fe_main.app.config["TESTING"] = True
    return fe_main.app.test_client()


@pytest.fixture()
def meta_conn(monkeypatch):
    """内存 file_meta：只提供 /api/list 读取的列，避免碰用户库。"""
    class _PersistentConnection(sqlite3.Connection):
        """路由的 finally 会 close()；测试要复用同一内存库，故 close 变空操作。"""

        def close(self) -> None:  # noqa: D401 - 测试替身
            return

    conn = sqlite3.connect(":memory:", factory=_PersistentConnection)
    conn.execute(
        """CREATE TABLE file_meta (
            path TEXT PRIMARY KEY, tags TEXT, comments TEXT, rating INTEGER,
            cached_info TEXT, color INTEGER, flag INTEGER, duration REAL,
            date_taken REAL, stack_id INTEGER, dom_color INTEGER,
            width INTEGER, height INTEGER, codec TEXT, missing INTEGER)"""
    )
    conn.execute("CREATE TABLE stacks (id INTEGER PRIMARY KEY, cover_path TEXT)")
    monkeypatch.setattr(fe_routes, "_db", lambda: conn)
    monkeypatch.setattr(fe_routes, "put_dir_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(fe_routes, "_locked_by", lambda path: None)
    yield conn
    conn.close()


def _headers():
    return {"X-Auth-Token": fe_main.APP_TOKEN}


def test_list_reports_total_and_request_id(tmp_path, client, meta_conn):
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.txt").write_text("hello", encoding="utf-8")

    response = client.get("/api/list", query_string={"path": str(tmp_path), "rid": "req-7"},
                          headers=_headers())
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == len(payload["files"]) == 2
    assert payload["truncated"] is False
    assert payload["request_id"] == "req-7"


def test_list_sends_structured_technical_fields(tmp_path, client, meta_conn):
    video = tmp_path / "clip.mov"
    video.write_bytes(b"x")
    meta_conn.execute(
        """INSERT INTO file_meta (path, tags, comments, rating, duration,
            width, height, codec, missing, color, flag)
           VALUES (?, '采访', '备注', 4, 65.0, 1920, 1080, 'ProRes 422', 0, 2, 1)""",
        (unicodedata.normalize("NFC", str(video)),),
    )
    meta_conn.commit()

    payload = client.get("/api/list", query_string={"path": str(tmp_path)},
                         headers=_headers()).get_json()
    entry = next(item for item in payload["files"] if item["name"] == "clip.mov")
    assert entry["width"] == 1920
    assert entry["height"] == 1080
    assert entry["codec"] == "ProRes 422"
    assert entry["duration"] == pytest.approx(65.0)
    assert entry["missing"] == 0
    assert entry["rating"] == 4
    assert entry["color"] == 2
    assert entry["flag"] == 1
    # 兼容字段仍在：前端旧渲染路径不受影响
    assert entry["size_bytes"] == 1
    assert "res" in entry and "t_ver" in entry and "mtime" in entry


def test_list_marks_missing_entries_instead_of_hiding_them(tmp_path, client, meta_conn):
    ghost = tmp_path / "ghost.mp4"
    ghost.write_bytes(b"x")
    meta_conn.execute(
        "INSERT INTO file_meta (path, missing, width, height) VALUES (?, 1, 3840, 2160)",
        (unicodedata.normalize("NFC", str(ghost)),),
    )
    meta_conn.commit()

    payload = client.get("/api/list", query_string={"path": str(tmp_path)},
                         headers=_headers()).get_json()
    entry = payload["files"][0]
    assert entry["missing"] == 1
    assert entry["width"] == 3840


def test_list_locked_directory_still_refuses(client, monkeypatch, tmp_path):
    monkeypatch.setattr(fe_routes, "_locked_by",
                        lambda path: {"path": str(tmp_path), "hint": "解锁后重试"})
    response = client.get("/api/list", query_string={"path": str(tmp_path)},
                          headers=_headers())
    assert response.status_code == 423
    payload = response.get_json()
    assert payload["locked"] is True
    assert payload["error"] == "目录已锁定"


def test_capabilities_are_consumable_and_require_auth(client):
    unauthorized = client.get("/api/capabilities")
    assert unauthorized.status_code in (401, 403)

    response = client.get("/api/capabilities", headers=_headers())
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["version"] >= 1
    capabilities = {item["key"]: item for item in payload["capabilities"]}
    for key in ("browse.list", "thumbnail", "metadata.patch", "preview.image"):
        assert capabilities[key]["available"] is True
    # 未接通的能力必须给出 reason，而不是静默 False
    unavailable = [item for item in payload["capabilities"] if not item["available"]]
    assert all(item["reason"] for item in unavailable)


def test_constants_present_for_chunking():
    assert const.THUMB_BATCH_CHUNK > 0


def _seed(conn, path, **fields):
    key = unicodedata.normalize("NFC", str(path))
    values = {"path": key, "tags": "", "comments": "", "rating": 0}
    values.update(fields)
    conn.execute(
        "INSERT INTO file_meta (path, tags, comments, rating, color, flag) VALUES (?,?,?,?,?,?)",
        (values["path"], values["tags"], values["comments"], values["rating"],
         values.get("color", 0), values.get("flag", 0)))
    conn.commit()
    return key


def test_batch_meta_applies_rating_zero_and_comments(tmp_path, client, meta_conn):
    """B06：rating=0 必须落库（取消评分），comments 一次批量写。"""
    first, second = tmp_path / "a.mov", tmp_path / "b.mov"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    key_a = _seed(meta_conn, first, rating=5, comments="旧备注")
    key_b = _seed(meta_conn, second, rating=4, comments="旧备注")

    response = client.post("/api/batch/meta", json={
        "files": [str(first), str(second)], "rating": 0, "comments": "统一备注",
    }, headers=_headers())
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    rows = meta_conn.execute(
        "SELECT path, rating, comments FROM file_meta WHERE path IN (?,?)", (key_a, key_b)
    ).fetchall()
    assert [(row[1], row[2]) for row in rows] == [(0, "统一备注"), (0, "统一备注")]


def test_batch_meta_only_touches_patched_fields(tmp_path, client, meta_conn):
    """字段级 patch：只给 color，不覆盖已有的 rating/tags/comments。"""
    path = tmp_path / "c.mov"
    path.write_bytes(b"x")
    key = _seed(meta_conn, path, rating=3, tags="保留", comments="不动")
    client.post("/api/batch/meta", json={"files": [str(path)], "color": 4}, headers=_headers())
    row = meta_conn.execute(
        "SELECT rating, tags, comments, color FROM file_meta WHERE path = ?", (key,)).fetchone()
    assert row == (3, "保留", "不动", 4)


def test_batch_meta_rejects_non_string_comments(tmp_path, client, meta_conn):
    path = tmp_path / "d.mov"
    path.write_bytes(b"x")
    _seed(meta_conn, path)
    response = client.post("/api/batch/meta", json={
        "files": [str(path)], "comments": {"bad": True},
    }, headers=_headers())
    assert response.status_code == 400
    assert "comments" in response.get_json()["message"]


def test_capabilities_expose_task_and_search_features(client):
    payload = client.get("/api/capabilities", headers=_headers()).get_json()
    keys = {item["key"]: item for item in payload["capabilities"]}
    for key in ("tasks", "browse.search", "file.move", "trash.restore"):
        assert keys[key]["available"] is True


def test_search_reports_total_and_pagination(tmp_path, client, meta_conn):
    """B05：全库搜索必须显式表达 offset/limit/total/truncated。"""
    for index in range(5):
        path = tmp_path / f"needle_{index}.mov"
        path.write_bytes(b"x")
        meta_conn.execute(
            "INSERT INTO file_meta (path, tags, comments, rating, color, missing) VALUES (?, ?, ?, ?, ?, 0)",
            (str(path), "stock", "", 3, 0))
    response = client.get("/api/search", query_string={
        "q": "needle", "limit": 2, "offset": 1, "rid": "search-1"}, headers=_headers())
    payload = response.get_json()
    assert payload["request_id"] == "search-1"
    assert payload["total"] >= 5
    assert len(payload["files"]) == 2
    assert payload["truncated"] is True
    assert payload["offset"] == 1 and payload["limit"] == 2


def test_task_endpoints_run_and_report(tmp_path, client):
    """B07：任务接口返回 job_id，逐项结果可轮询，非法操作不产生任务。"""
    (tmp_path / "src.mov").write_bytes(b"x")
    response = client.post("/api/tasks/start", json={
        "operation": "copy", "targets": [str(tmp_path / "src.mov")],
        "policy": "error", "options": {"destination": str(tmp_path / "out")}},
        headers=_headers())
    assert response.status_code == 200
    job = response.get_json()["job"]
    assert job["job_id"] and job["operation"] == "copy"
    (tmp_path / "out").mkdir(exist_ok=True)
    status = client.get("/api/tasks/status", query_string={"job_id": job["job_id"]},
                        headers=_headers()).get_json()["job"]
    assert status["status"] in ("queued", "running", "failed", "completed")
    assert status["total"] == 1

    bad = client.post("/api/tasks/start", json={"operation": "format_disk",
                                                "targets": [str(tmp_path)]},
                      headers=_headers())
    assert bad.status_code == 400
    assert client.get("/api/tasks/status", query_string={"job_id": "missing"},
                      headers=_headers()).status_code == 404


def test_bx_guard_services_remain_available(client):
    """BX02–BX05 复用既有端点；退役/迁移前必须先证明这些入口存在。"""
    rules = {rule.rule for rule in fe_main.app.url_map.iter_rules()}
    for endpoint in (
        "/api/meta/export", "/api/meta/import", "/api/meta/relink",
        "/api/export/run", "/api/manager/plugins",
        "/api/ocr/status", "/api/ocr/run", "/api/similar",
        "/api/map/files", "/api/stats",
        "/api/tasks/start", "/api/tasks/status", "/api/tasks/cancel", "/api/tasks/list",
        "/api/search", "/api/scan/cancel", "/api/library/backup",
    ):
        assert endpoint in rules, endpoint
