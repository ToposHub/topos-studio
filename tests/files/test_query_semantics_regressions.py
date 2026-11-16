"""NR-02 回归（后端）：查询分页与标签语义（FR-10 / FR-11）。

复用文件安全回归的隔离接线：routes 指向临时 SQLite，磁盘存在性用
/audit-search/ 前缀替身（不需要 2001 个真实文件即可验证分页切片语义）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILE_EXPLORER = ROOT / "external" / "file_explorer"
if str(FILE_EXPLORER) not in sys.path:
    sys.path.insert(0, str(FILE_EXPLORER))

from tests.files.test_file_safety_regressions import backend  # noqa: E402,F401


def _headers(backend):
    return {"X-Auth-Token": backend.main.APP_TOKEN, "Content-Type": "application/json"}


def _client(backend):
    backend.main.app.config["TESTING"] = True
    return backend.main.app.test_client(), _headers(backend)


def _seed_search_paths(backend, count: int) -> None:
    conn = backend.connect()
    conn.executemany(
        "INSERT INTO file_meta(path,tags,rating,missing) VALUES(?,?,?,0)",
        [(f"/audit-search/{i:04d}.mov", "", 0) for i in range(count)])
    conn.commit()
    conn.close()


def _mock_disk_paths(backend, monkeypatch):
    """2001 条虚拟路径：exists/stat 全部命中，不建真实文件。"""
    routes = backend.routes
    original_exists, original_stat = routes.os.path.exists, routes.os.stat

    def exists(path):
        return True if str(path).startswith("/audit-search/") else original_exists(path)

    def stat(path, *args, **kwargs):
        if str(path).startswith("/audit-search/"):
            return SimpleNamespace(st_size=1, st_mtime=1700000000)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(routes.os.path, "exists", exists)
    monkeypatch.setattr(routes.os, "stat", stat)


# ---------------------------------------------------------------------------
# FR-10：分页在全匹配集合上进行；末页可达、计数一致
# ---------------------------------------------------------------------------

def test_fr10_offset_beyond_legacy_cap_returns_last_entry(tmp_path, backend, monkeypatch):
    _seed_search_paths(backend, 2001)
    _mock_disk_paths(backend, monkeypatch)
    client, headers = _client(backend)
    response = client.get("/api/search",
                          query_string={"q": "audit-search", "offset": 2000, "limit": 10},
                          headers=headers)
    assert response.status_code == 200
    payload = response.get_json()
    # 此前先 [:2000] 再分页：offset=2000 返回空页但 total=2001
    assert payload["total"] == 2001
    assert len(payload["files"]) == 1           # 第 2001 条可达
    assert payload["truncated"] is False


def test_fr10_paging_is_complete_and_stable(tmp_path, backend, monkeypatch):
    _seed_search_paths(backend, 205)
    _mock_disk_paths(backend, monkeypatch)
    client, headers = _client(backend)
    seen, offset = [], 0
    while True:
        payload = client.get("/api/search",
                             query_string={"q": "audit-search", "offset": offset, "limit": 50},
                             headers=headers).get_json()
        seen.extend(item["path"] for item in payload["files"])
        if not payload["truncated"]:
            break
        offset += 50
    assert len(seen) == 205                       # 无漏
    assert len(set(seen)) == 205                  # 无重
    assert seen == sorted(seen)                   # 稳定排序


# ---------------------------------------------------------------------------
# FR-11：tags_op 显式语义 + 最终快照返回
# ---------------------------------------------------------------------------

def _file_meta_row(backend, path: str):
    conn = backend.connect()
    row = conn.execute("SELECT tags FROM file_meta WHERE path = ?", (path,)).fetchone()
    conn.close()
    return row


def test_fr11_update_meta_replace_semantics_and_snapshot(tmp_path, backend):
    media = tmp_path / "a.mov"
    media.write_text("x")
    conn = backend.connect()
    conn.execute("INSERT INTO file_meta(path,tags) VALUES(?, 'old,keep')", (str(media),))
    conn.commit()
    conn.close()

    client, headers = _client(backend)
    response = client.post("/api/update_meta",
                           json={"path": str(media), "tags": "new,keep",
                                 "tags_op": "replace"},
                           headers=headers)
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["user"]["tags"] == "keep,new"      # 最终快照（排序去重）
    assert _file_meta_row(backend, str(media)) == ("keep,new",)


def test_fr11_batch_meta_legacy_add_default_and_explicit_replace(tmp_path, backend):
    a = tmp_path / "a.mov"
    b = tmp_path / "b.mov"
    a.write_text("x")
    b.write_text("x")
    conn = backend.connect()
    conn.execute("INSERT INTO file_meta(path,tags) VALUES(?, 'old')", (str(a),))
    conn.execute("INSERT INTO file_meta(path,tags) VALUES(?, 'old')", (str(b),))
    conn.commit()
    conn.close()

    client, headers = _client(backend)
    # 不传 tags_op：历史默认 add（旧前端兼容）
    payload = client.post("/api/batch/meta",
                          json={"files": [str(a)], "tags": "extra"},
                          headers=headers).get_json()
    assert payload["success"] is True
    assert payload["results"][0]["user"]["tags"] == "extra,old"

    # 原生面板显式 replace：输入即最终值
    payload = client.post("/api/batch/meta",
                          json={"files": [str(a), str(b)], "tags": "final",
                                "tags_op": "replace"},
                          headers=headers).get_json()
    assert payload["success"] is True
    by_path = {item["path"]: item["user"]["tags"] for item in payload["results"]}
    assert by_path == {str(a): "final", str(b): "final"}


def test_fr11_clear_and_remove_semantics(tmp_path, backend):
    media = tmp_path / "c.mov"
    media.write_text("x")
    conn = backend.connect()
    conn.execute("INSERT INTO file_meta(path,tags) VALUES(?, 'keep,drop,other')",
                 (str(media),))
    conn.commit()
    conn.close()

    client, headers = _client(backend)
    payload = client.post("/api/update_meta",
                          json={"path": str(media), "tags": "drop",
                                "tags_op": "remove"},
                          headers=headers).get_json()
    assert payload["user"]["tags"] == "keep,other"
    payload = client.post("/api/update_meta",
                          json={"path": str(media), "tags": "",
                                "tags_op": "clear"},
                          headers=headers).get_json()
    assert payload["user"]["tags"] == ""
    assert _file_meta_row(backend, str(media)) == ("",)


def test_fr11_structured_file_tags_follow_replace(tmp_path, backend):
    """replace/remove/clear 后 file_tags 关系表不得残留被移除的标签。"""
    media = tmp_path / "d.mov"
    media.write_text("x")
    conn = backend.connect()
    conn.execute("INSERT INTO file_meta(path,tags) VALUES(?, 'a,b')", (str(media),))
    for tag in ("a", "b"):
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
        conn.execute("""INSERT OR IGNORE INTO file_tags (file_path, tag_id)
                        SELECT ?, id FROM tags WHERE name = ?""", (str(media), tag))
    conn.commit()
    conn.close()

    client, headers = _client(backend)
    client.post("/api/update_meta",
                json={"path": str(media), "tags": "a", "tags_op": "replace"},
                headers=headers)
    conn = backend.connect()
    rows = conn.execute(
        """SELECT t.name FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
           WHERE ft.file_path = ? ORDER BY t.name""", (str(media),)).fetchall()
    conn.close()
    assert rows == [("a",)]  # b 已从关系表移除，不再是幽灵标签
