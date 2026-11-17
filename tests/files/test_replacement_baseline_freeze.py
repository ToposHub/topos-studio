"""Files 替代冻结基线（A 阶段第 4 项）：临时库 + 合成素材冻结当前生产行为。

对应[总计划](docs/plans/2026-11-17-files-final-replacement-and-upgrade-plan.md) A 阶段
"以临时库和合成素材冻结回归：浏览、写 metadata、改名/移动、删恢复"。
工程导入与播放器关闭两项由既有冻结测试承担（见
docs/validation/files_replacement_phase_a_20261117/README.md 的映射表），
本模块不重复 Qt 侧用例。

口径：这些测试冻结**旧系统当前行为**（真实 Flask 应用 + 真实临时 SQLite +
真实文件系统），B/C 阶段替换实现时按[能力矩阵](docs/plans/files-replacement-capability-matrix-20261117.md)
逐项对齐；I 阶段旧目录退役时改写为新架构门禁，不是删除能力证据。

隔离：DB/config/回收站全部重定向到 pytest 临时目录（db.py 的
`_topos_db_path`/`_fallback_db_path`/`CONFIG_FILE` 模块全局与
`trash.get_trash_dir` 是既有的测试缝），不触碰仓库 topos.db 与用户素材。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILE_EXPLORER = ROOT / "external" / "file_explorer"
if str(FILE_EXPLORER) not in sys.path:
    sys.path.insert(0, str(FILE_EXPLORER))

pytest.importorskip("PyQt6.QtWebEngineCore")
pytest.importorskip("PIL")

import main as fe_main  # noqa: E402
import db as fe_db  # noqa: E402
import trash as fe_trash  # noqa: E402

POLL_TIMEOUT_S = 10.0


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", str(text))


class _FrozenLibrary:
    """临时库上下文：合成素材 + 重定向后的 DB/config/回收站。"""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.lib = tmp_path / "library"
        (self.lib / "sub").mkdir(parents=True)
        self._make_png(self.lib / "a.png", (8, 8), (255, 0, 0))
        self._make_png(self.lib / "b.jpg", (12, 6), (0, 128, 255))
        (self.lib / "notes.txt").write_text("合成文本素材", encoding="utf-8")
        self._make_png(self.lib / "sub" / "c.png", (4, 4), (0, 0, 0))

        self.db_file = tmp_path / "data" / "topos.db"
        self.db_file.parent.mkdir()
        self.db_file.touch()
        self.config_file = tmp_path / "data" / "config.json"
        self.trash_dir = tmp_path / "data" / "trash"

    @staticmethod
    def _make_png(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
        from PIL import Image

        fmt = {"jpg": "JPEG"}.get(path.suffix.lstrip(".").lower(),
                                  path.suffix.lstrip(".").upper())
        Image.new("RGB", size, color).save(path, format=fmt)

    def db_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def meta_row(self, path: Path | str) -> sqlite3.Row | None:
        with self.db_connect() as conn:
            return conn.execute(
                "SELECT * FROM file_meta WHERE path = ?", (_nfc(path),)
            ).fetchone()


@pytest.fixture()
def frozen_library(tmp_path, monkeypatch):
    """把旧应用整体指到临时库；恢复后不影响其他用例。"""
    frozen = _FrozenLibrary(tmp_path)
    monkeypatch.setattr(fe_db, "_topos_db_path", str(frozen.db_file))
    monkeypatch.setattr(fe_db, "_fallback_db_path", str(tmp_path / "data" / "metadata.db"))
    monkeypatch.setattr(fe_db, "CONFIG_FILE", str(frozen.config_file))

    def _trash_dir() -> str:
        frozen.trash_dir.mkdir(parents=True, exist_ok=True)  # 对齐原函数的 makedirs
        return str(frozen.trash_dir)

    monkeypatch.setattr(fe_trash, "get_trash_dir", _trash_dir)
    fe_db.DIR_CACHE.clear()
    fe_main.init_db()
    # scanner._libraries 读取 [{'path': ...}] 字典形状（scanner.py:78-80）
    fe_db.save_config({"libraries": [{"path": str(frozen.lib)}], "meta_embed": "off"})
    fe_main.app.config["TESTING"] = True
    yield frozen
    fe_db.DIR_CACHE.clear()


@pytest.fixture()
def client(frozen_library):
    return fe_main.app.test_client()


def _headers() -> dict[str, str]:
    return {"X-Auth-Token": fe_main.APP_TOKEN}


def _wait_task(client, job_id: str) -> dict:
    """轮询任务到终态，返回终态 job；超时抛 AssertionError。"""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        job = client.get("/api/tasks/status", query_string={"job_id": job_id},
                         headers=_headers()).get_json()["job"]
        if job.get("status") in ("completed", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"任务 {job_id} 未在 {POLL_TIMEOUT_S}s 内到达终态")


def _run_task(client, operation: str, targets: list[str],
              options: dict | None = None) -> dict:
    response = client.post("/api/tasks/start", json={
        "operation": operation, "targets": targets,
        "policy": "error", "options": dict(options or {})}, headers=_headers())
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["success"] is True
    job = _wait_task(client, payload["job"]["job_id"])
    assert job["status"] == "completed", job
    return job


def _wait_scan_idle(client) -> None:
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        state = client.get("/api/scan/status", headers=_headers()).get_json()
        if not state.get("running"):
            return
        time.sleep(0.05)
    raise AssertionError(f"扫描未在 {POLL_TIMEOUT_S}s 内结束")


# ---------------------------------------------------------------- 浏览


def test_freeze_browse_lists_synthetic_library(client, frozen_library):
    """/api/list 冻结：目录条目、total、technical 兼容字段。"""
    payload = client.get("/api/list", query_string={"path": str(frozen_library.lib)},
                         headers=_headers()).get_json()
    names = {item["name"] for item in payload["files"]}
    assert {"a.png", "b.jpg", "notes.txt", "sub"} <= names
    assert payload["total"] == len(payload["files"])
    assert payload["truncated"] is False
    entry = next(item for item in payload["files"] if item["name"] == "a.png")
    assert entry["size_bytes"] > 0
    assert entry["missing"] == 0
    assert "mtime" in entry and "res" in entry


def test_freeze_tree_lists_child_directories(client, frozen_library):
    payload = client.get("/api/tree", query_string={"path": str(frozen_library.lib)},
                         headers=_headers()).get_json()
    assert {"sub"} == {node["name"] for node in payload}


def test_freeze_scan_indexes_files_with_size(client, frozen_library):
    """/api/scan 冻结：注册库扫描后 file_meta 有技术行。"""
    assert client.post("/api/scan/start", headers=_headers()).get_json()["success"] is True
    _wait_scan_idle(client)
    row = frozen_library.meta_row(frozen_library.lib / "a.png")
    assert row is not None
    assert row["size"] > 0
    assert row["width"] == 8 and row["height"] == 8
    assert row["missing"] == 0


# ---------------------------------------------------------------- 写 metadata


def test_freeze_update_meta_persists_user_fields_and_fts(client, frozen_library):
    """update_meta 冻结：DB 行 + FTS 可检 + 响应携带最终快照。"""
    target = str(frozen_library.lib / "a.png")
    response = client.post("/api/update_meta", json={
        "path": target, "rating": 4, "color": 2, "comments": "基线备注alpha"},
        headers=_headers())
    assert response.status_code == 200
    assert response.get_json()["success"] is True

    row = frozen_library.meta_row(target)
    assert row["rating"] == 4 and row["color"] == 2
    assert row["comments"] == "基线备注alpha"

    result = client.get("/api/search", query_string={"q": "基线备注alpha"},
                        headers=_headers()).get_json()
    assert any(str(item.get("path")) == _nfc(target) for item in result["files"])


def test_freeze_update_meta_sidecar_mode_writes_xmp(client, frozen_library):
    """meta_embed=sidecar 冻结：标记写回素材旁 .xmp（用户数据政策）。

    写回经 exiftool（meta_sync）；工具链缺失时旧系统静默跳过——该依赖
    已登记进能力矩阵 §1 行 15，此处仅在工具链存在的环境冻结落盘行为。
    """
    if not shutil.which("exiftool"):
        pytest.skip("exiftool 不在本机：sidecar 写回在旧系统被静默跳过（矩阵已登记依赖）")
    fe_db.save_config({"meta_embed": "sidecar"})
    target = frozen_library.lib / "a.png"
    response = client.post("/api/update_meta", json={
        "path": str(target), "rating": 5}, headers=_headers())
    assert response.status_code == 200
    sidecar = target.with_suffix(".png.xmp")
    assert sidecar.exists(), "sidecar 模式必须产出 .xmp"
    assert b"Rating" in sidecar.read_bytes()


def test_freeze_batch_meta_only_touches_patched_fields(client, frozen_library):
    target = str(frozen_library.lib / "b.jpg")
    client.post("/api/update_meta", json={"path": target, "rating": 3},
                headers=_headers())
    response = client.post("/api/batch/meta", json={
        "files": [target], "color": 1}, headers=_headers())
    assert response.status_code == 200
    row = frozen_library.meta_row(target)
    assert row["rating"] == 3, "批量的 color 不应清掉既有 rating"
    assert row["color"] == 1


# ---------------------------------------------------------------- 改名/移动


def test_freeze_rename_moves_file_and_metadata_together(client, frozen_library):
    target = frozen_library.lib / "a.png"
    client.post("/api/update_meta", json={"path": str(target), "rating": 4},
                headers=_headers())

    _run_task(client, "rename", [str(target)], {"new_name": "renamed.png"})

    assert not target.exists()
    new_path = frozen_library.lib / "renamed.png"
    assert new_path.exists()
    row = frozen_library.meta_row(new_path)
    assert row is not None and row["rating"] == 4, "改名后用户标记随行"
    assert frozen_library.meta_row(target) is None


def test_freeze_move_into_subdirectory_keeps_identity(client, frozen_library):
    target = frozen_library.lib / "b.jpg"
    client.post("/api/update_meta", json={"path": str(target), "rating": 2},
                headers=_headers())

    _run_task(client, "move", [str(target)],
              {"destination": str(frozen_library.lib / "sub")})

    assert not target.exists()
    moved = frozen_library.lib / "sub" / "b.jpg"
    assert moved.exists()
    row = frozen_library.meta_row(moved)
    assert row is not None and row["rating"] == 2
    assert frozen_library.meta_row(target) is None


# ---------------------------------------------------------------- 删/恢复


def test_freeze_trash_then_restore_roundtrip(client, frozen_library):
    """回收站冻结：入站有快照、恢复后文件与用户标记原样回来。"""
    target = frozen_library.lib / "a.png"
    client.post("/api/update_meta", json={
        "path": str(target), "rating": 5, "comments": "回收站快照"}, headers=_headers())

    _run_task(client, "trash", [str(target)])
    assert not target.exists()
    with frozen_library.db_connect() as conn:
        rows = conn.execute("SELECT * FROM trash").fetchall()
    assert len(rows) == 1
    assert "回收站快照" in (rows[0]["meta_json"] or "")
    assert any(frozen_library.trash_dir.rglob("*.png"))

    _run_task(client, "restore", [str(target)])
    assert target.exists()
    row = frozen_library.meta_row(target)
    assert row is not None
    assert row["rating"] == 5 and row["comments"] == "回收站快照"
    with frozen_library.db_connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM trash").fetchone()[0] == 0


def test_freeze_purge_removes_permanently(client, frozen_library):
    target = frozen_library.lib / "b.jpg"
    _run_task(client, "trash", [str(target)])
    _run_task(client, "purge", [str(target)])
    assert not target.exists()
    assert frozen_library.meta_row(target) is None
    with frozen_library.db_connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM trash").fetchone()[0] == 0


# ---------------------------------------------------------------- 任务通道语义


def test_freeze_task_rejects_unknown_operation(client, frozen_library):
    response = client.post("/api/tasks/start", json={
        "operation": "format_disk", "targets": [str(frozen_library.lib)]},
        headers=_headers())
    assert response.status_code == 400


def test_freeze_capabilities_surface_stable(client, frozen_library):
    """/api/capabilities 冻结：原生按此启用操作（transport:1041）。"""
    payload = client.get("/api/capabilities", headers=_headers()).get_json()
    keys = {item["key"]: item for item in payload["capabilities"]}
    for key in ("tasks", "browse.search", "file.move", "trash.restore"):
        assert keys[key]["available"] is True
