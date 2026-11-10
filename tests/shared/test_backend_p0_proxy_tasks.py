"""P0-7 代理/优化媒体任务——后端核验与补齐验收。

对应《媒体池后端交接计划》P0 第 7 项：
- 任务队列/并发限制/取消/挂起恢复：复用既有实现（AUD-35607/35610 等）
- 本轮补齐：磁盘空间预检（不足直接失败、不落半成品）、失败显式重试
  （保留合并回调，不自动重试）、缓存产物类型域声明（proxy vs 未来
  optimized media，旧清单读回 "proxy"）
- 验收：不在导入时自动启动昂贵任务（策略层 evaluate_proxy_action 把关）；
  项目关闭后无遗留任务（shutdown/取消路径）
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.shared.media.disk_proxy_service import (
    PROXY_MIN_FREE_RESERVE_BYTES,
    DiskProxyService,
    check_free_space,
    estimate_proxy_reserve_bytes,
)
from src.shared.media.proxy import DiskProxyConfig
from src.shared.media.proxy_manifest import ProxyManifest, ProxyManifestEntry
from src.shared.media.proxy_manager import ProxyManager

TEST_VIDEOS = Path(__file__).resolve().parents[2] / "tests" / "test_videos"


# ================================================================
# 磁盘空间预检
# ================================================================

def test_reserve_estimate_conservative(tmp_path) -> None:
    """预留估算 = max(源大小, 512MiB)；长 GOP 源转码可能大于源。"""
    media = tmp_path / "small.mp4"
    media.write_bytes(b"x" * 1024)
    assert estimate_proxy_reserve_bytes(str(media)) == PROXY_MIN_FREE_RESERVE_BYTES
    big = tmp_path / "big.mov"
    big.write_bytes(b"x" * (PROXY_MIN_FREE_RESERVE_BYTES + 1))
    assert estimate_proxy_reserve_bytes(str(big)) == big.stat().st_size
    # 源不存在（离线）也能给出下限预留
    assert estimate_proxy_reserve_bytes(str(tmp_path / "ghost.mov")) == \
        PROXY_MIN_FREE_RESERVE_BYTES


def test_check_free_space_walks_to_existing_parent(tmp_path) -> None:
    ok, free = check_free_space(str(tmp_path / "not" / "yet" / "created"), 1)
    assert ok is True and free > 0
    ok2, free2 = check_free_space(str(tmp_path), 2**60)
    assert ok2 is False and free2 == free


def test_generate_proxy_fails_fast_on_insufficient_space(
        tmp_path, monkeypatch) -> None:
    """空间不足：不启动编码、状态 FAILED、on_error 带明确原因。"""
    media = TEST_VIDEOS / "2K_h264.mp4"
    if not media.exists():
        pytest.skip(f"测试素材缺失: {media}")
    service = DiskProxyService(DiskProxyConfig(cache_dir=str(tmp_path / "cache")))
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.shared.media.disk_proxy_service.check_free_space",
        lambda directory, required: (False, 1024))
    task_id = service.generate_proxy(
        str(media), "asset-1", on_error=lambda path, msg: errors.append((path, msg)))
    assert task_id is None  # 未入队
    assert len(errors) == 1
    assert "Insufficient disk space" in errors[0][1]
    info = service.get_disk_proxy_info(str(media))
    assert info.status.value == "failed"
    assert info.error_message and "disk space" in info.error_message
    # 编码器从未启动：缓存目录里没有产物
    assert not list((tmp_path / "cache").rglob("*.mov"))


# ================================================================
# 失败重试
# ================================================================

def _wait_status(manager: ProxyManager, task_id: str, statuses: set,
                 timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.get_task_status(task_id)
        if status is not None and str(getattr(status, "status", status)) in statuses:
            return str(getattr(status, "status", status))
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} 未达终态 {statuses}")


def test_retry_failed_requeues_with_callbacks(tmp_path) -> None:
    """失败任务显式重试：新任务 ID、原失败回调保留并被再次触发。"""
    manager = ProxyManager(DiskProxyConfig(cache_dir=str(tmp_path / "cache")))
    # 不存在的源 → 生成失败
    errors: list[str] = []
    task_id = manager.queue_proxy_generation(
        asset_id="a1", source_path=str(tmp_path / "ghost.mov"),
        on_error=lambda msg: errors.append(msg))
    assert task_id is not None
    _wait_status(manager, task_id, {"failed"})
    assert manager.failed_sources() == [str(tmp_path / "ghost.mov")]

    new_task_id = manager.retry_failed(str(tmp_path / "ghost.mov"))
    assert new_task_id is not None and new_task_id != task_id
    _wait_status(manager, new_task_id, {"failed"})
    # 重试后的失败再次触发原回调（不自动无限重试）
    assert len(errors) >= 2
    # 无失败记录的源 → None（调用方自行 queue）
    assert manager.retry_failed(str(tmp_path / "never-queued.mov")) is None


def test_retry_returns_none_for_completed_or_missing(tmp_path) -> None:
    manager = ProxyManager(DiskProxyConfig(cache_dir=str(tmp_path / "cache")))
    assert manager.retry_failed(str(tmp_path / "nothing.mov")) is None


# ================================================================
# 缓存产物类型域（proxy vs optimized）
# ================================================================

def test_manifest_kind_field_roundtrip_and_legacy_default(tmp_path) -> None:
    from src.shared.media.proxy_manifest import ProxyManifestKey, ProxyStatus
    import json
    import os

    media = TEST_VIDEOS / "2K_h264.mp4"
    if not media.exists():
        pytest.skip(f"测试素材缺失: {media}")
    stat = os.stat(media)

    manifest = ProxyManifest(cache_dir=tmp_path)
    key = ProxyManifestKey(
        source_path=str(media), source_mtime_ns=stat.st_mtime_ns,
        source_size=stat.st_size, codec_policy="auto",
        resolution_policy="half", app_version="test")
    entry = ProxyManifestEntry(
        key=key, status=ProxyStatus.READY, proxy_path=str(tmp_path / "p.mov"),
        width=960, height=540, codec="prores_ks", pixel_format="yuv422p10le",
        created_at=1.0, updated_at=1.0)
    assert entry.kind == "proxy"  # 现有唯一产物类型
    manifest.upsert_entry(entry)
    manifest.save()

    # 序列化形状里带上了 kind 声明；读回往返一致
    reopened = ProxyManifest(cache_dir=tmp_path)
    reopened.load()
    got = reopened.get_entry(str(media))
    assert got is not None and got.kind == "proxy"
    raw = json.loads((tmp_path / ProxyManifest.MANIFEST_FILENAME).read_text())
    values = raw.get("entries", raw) if isinstance(raw, dict) else raw
    if isinstance(values, dict):
        assert any(v.get("kind") == "proxy" for v in values.values()
                   if isinstance(v, dict))
    # 旧清单（手写无 kind 键）读回默认 "proxy"
    legacy = raw if isinstance(raw, dict) else {}
    if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
        for v in raw["entries"].values():
            if isinstance(v, dict):
                v.pop("kind", None)
        (tmp_path / ProxyManifest.MANIFEST_FILENAME).write_text(
            json.dumps(raw))
        legacy_manifest = ProxyManifest(cache_dir=tmp_path)
        legacy_manifest.load()
        legacy_entry = legacy_manifest.get_entry(str(media))
        assert legacy_entry is not None and legacy_entry.kind == "proxy"


def test_cancel_all_tasks_leaves_no_active_work(tmp_path) -> None:
    """取消全部：所有任务离开 queued/generating（无遗留工作）。"""
    manager = ProxyManager(DiskProxyConfig(cache_dir=str(tmp_path / "cache")))
    ids = []
    for index in range(3):
        ids.append(manager.queue_proxy_generation(
            asset_id=f"a{index}",
            source_path=str(tmp_path / f"missing-{index}.mov")))
    manager.cancel_all_tasks()
    for task_id in ids:
        if task_id is None:
            continue
        status = str(getattr(manager.get_task_status(task_id), "status", ""))
        assert status not in ("queued", "generating")
