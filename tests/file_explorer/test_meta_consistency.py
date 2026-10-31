"""R1-1/R1-3/R1-4 回归测试：file_meta 数据一致性（NFC 键空间 + 卷判定）。

- R1-1: _volume_available 必须归约到 /Volumes/<卷> 根再判定存在性
  （此前首次循环即用完整文件路径判定，调用前提"文件不存在"导致恒 False，
  外接卷孤儿 meta 永不清理）。
- R1-3: 删除 NFD 孪生行前必须合并其非空 tags/comments/rating 到 NFC 行。
- R1-4: 宿主侧 set/get_file_rating 必须与 fe 侧一样以 NFC 为键。
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
from src.shared.db.database import Database  # noqa: E402

NFD = unicodedata.normalize


# ---------------------------------------------------------------------------
# R1-1 卷可用性判定
# ---------------------------------------------------------------------------

def test_volume_available_checks_volume_root(monkeypatch):
    """文件不存在但卷根存在 → 可用（恒 False 即为回归）。"""
    existing = {"/Volumes/EXT"}

    def fake_exists(p):
        return p in existing

    monkeypatch.setattr(fe_main.os.path, "exists", fake_exists)

    missing_file = "/Volumes/EXT/projects/a/b.mp4"
    assert not fake_exists(missing_file), "测试前提：文件本身不存在"
    assert fe_main._volume_available(missing_file) is True


def test_volume_unavailable_when_volume_root_missing(monkeypatch):
    def fake_exists(p):
        return False

    monkeypatch.setattr(fe_main.os.path, "exists", fake_exists)
    assert fe_main._volume_available("/Volumes/EXT/a/b.mp4") is False


def test_internal_volume_always_available(monkeypatch):
    def fake_exists(p):
        return False

    monkeypatch.setattr(fe_main.os.path, "exists", fake_exists)
    assert fe_main._volume_available("/Users/testuser/missing/file.mp4") is True


# ---------------------------------------------------------------------------
# R1-3 NFD 孪生行合并
# ---------------------------------------------------------------------------

@pytest.fixture()
def meta_db():
    conn = sqlite3.connect(":memory:")
    conn.execute('''CREATE TABLE file_meta (
        path TEXT PRIMARY KEY, tags TEXT, comments TEXT,
        rating INTEGER DEFAULT 0, cached_info TEXT)''')
    yield conn
    conn.close()


def _insert(conn, path, tags="", comments="", rating=0):
    conn.execute(
        "INSERT INTO file_meta (path, tags, comments, rating) VALUES (?, ?, ?, ?)",
        (path, tags, comments, rating),
    )


def test_absorb_nfd_twin_merges_user_data(meta_db):
    nfc = "café/video.mp4"
    nfd = NFD("NFD", nfc)
    _insert(meta_db, nfc)                       # NFC 行：空数据
    _insert(meta_db, nfd, tags="风景", comments="好片", rating=5)  # NFD 行：宿主所写

    fe_main._absorb_nfd_twin(meta_db, nfc, nfd)
    meta_db.commit()

    row = meta_db.execute(
        "SELECT tags, comments, rating FROM file_meta WHERE path = ?", (nfc,)
    ).fetchone()
    assert row == ("风景", "好片", 5)
    assert meta_db.execute(
        "SELECT 1 FROM file_meta WHERE path = ?", (nfd,)
    ).fetchone() is None


def test_absorb_nfd_twin_never_overwrites_nfc_data(meta_db):
    nfc = "café/video.mp4"
    nfd = NFD("NFD", nfc)
    _insert(meta_db, nfc, tags="已有", rating=4)
    _insert(meta_db, nfd, tags="旧的", comments="旧评", rating=2)

    fe_main._absorb_nfd_twin(meta_db, nfc, nfd)
    meta_db.commit()

    row = meta_db.execute(
        "SELECT tags, comments, rating FROM file_meta WHERE path = ?", (nfc,)
    ).fetchone()
    assert row == ("已有", "旧评", 4), "NFC 行已有数据不得被 NFD 行覆盖"


# ---------------------------------------------------------------------------
# R1-4 宿主侧 NFC 键
# ---------------------------------------------------------------------------

def test_host_rating_uses_nfc_key(tmp_path):
    db = Database(str(tmp_path / "topos.db"))
    nfc = "café/photo.raw"
    nfd = NFD("NFD", nfc)

    db.set_file_rating(nfd, 5)
    # NFC 键读取必须命中（此前宿主以 NFD 写入 → fe 侧 NFC 查询 miss）
    assert db.get_file_rating(nfc) == 5
    assert db.get_file_rating(nfd) == 5

    row_count = 0
    conn = sqlite3.connect(str(tmp_path / "topos.db"))
    for (p,) in conn.execute("SELECT path FROM file_meta"):
        assert p == nfc, f"库里只应存在 NFC 键，实际: {p!r}"
        row_count += 1
    conn.close()
    assert row_count == 1, "NFD/NFC 双行即回归"
