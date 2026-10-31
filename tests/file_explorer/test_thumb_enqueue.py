"""R0-1 回归测试：/api/thumb 请求未命中缓存时必须入队异步生成。

历史回归：入队块曾被缩进到 ``if os.path.exists(target):`` 内部，
导致目标缓存不存在时整块跳过、永不入队——冷启动/新目录的视频大图
永远停留在占位图。本测试锁定"缓存不存在 → 请求入队"这一行为。
"""

from __future__ import annotations

import os
import queue
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILE_EXPLORER = ROOT / "external" / "file_explorer"
if str(FILE_EXPLORER) not in sys.path:
    sys.path.insert(0, str(FILE_EXPLORER))

pytest.importorskip("PyQt6.QtWebEngineCore")

import main as fe_main  # noqa: E402


@pytest.fixture()
def thumb_client():
    fe_main.app.config["TESTING"] = True
    return fe_main.app.test_client()


def _drain_queue(q: "queue.Queue") -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


@pytest.fixture()
def clean_thumb_queue():
    with fe_main.thumbnail_lock:
        fe_main.thumbnail_pending.clear()
    _drain_queue(fe_main.thumbnail_queue)
    yield
    with fe_main.thumbnail_lock:
        fe_main.thumbnail_pending.clear()
    _drain_queue(fe_main.thumbnail_queue)


def test_missing_cache_enqueues_generation(thumb_client, clean_thumb_queue, tmp_path):
    """缓存 target 不存在时，/api/thumb 必须把请求加入生成队列。"""
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 2048)  # 视频扩展名不走小图直读分支，内容无需可解码
    path = str(src)

    target = fe_main.thumb_engine.get_thumb_path(path, size_type="small")
    if os.path.exists(target):
        os.remove(target)

    resp = thumb_client.get(
        "/api/thumb",
        query_string={"token": fe_main.APP_TOKEN, "path": path, "size": "small"},
    )
    # 未生成时返回占位图（static 资源存在→200）或 202
    assert resp.status_code in (200, 202)

    with fe_main.thumbnail_lock:
        assert (path, "small") in fe_main.thumbnail_pending
        assert not fe_main.thumbnail_queue.empty()
        queued = fe_main.thumbnail_queue.get_nowait()
    assert queued == (path, target, "small")


def test_existing_fresh_cache_does_not_enqueue(thumb_client, clean_thumb_queue, tmp_path):
    """缓存命中且未 stale 时直接返回缓存文件，不重复入队。"""
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 2048)
    path = str(src)

    target = fe_main.thumb_engine.get_thumb_path(path, size_type="small")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(b"fake-thumb")

    resp = thumb_client.get(
        "/api/thumb",
        query_string={"token": fe_main.APP_TOKEN, "path": path, "size": "small"},
    )
    assert resp.status_code == 200

    with fe_main.thumbnail_lock:
        assert (path, "small") not in fe_main.thumbnail_pending
        assert fe_main.thumbnail_queue.empty()
