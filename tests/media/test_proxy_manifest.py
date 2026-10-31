"""
Stage D unit tests: ProxyManifest + AssetProxyManager.
对应规格书 §7 / §20.1 test_proxy_manifest.py。

核心验收：
- 同源素材多次 ensure 只生成一个 entry（去重）。
- 查询 <1ms（纯内存）。
- manifest 持久化 + 加载。
- 源文件 mtime/size 变化 → 失效。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.media.proxy import DiskProxyConfig, ProxyStatus
from src.shared.media.proxy_manifest import (
    ProxyManifest,
    ProxyManifestEntry,
    ProxyManifestKey,
    build_manifest_key,
)
from src.shared.media.asset_proxy_manager import AssetProxyManager


# =============================================================================
# Helpers
# =============================================================================


def _make_key(source_path: str = "/a/b.mov", mtime: int = 1000, size: int = 999) -> ProxyManifestKey:
    return ProxyManifestKey(
        source_path=source_path,
        source_mtime_ns=mtime,
        source_size=size,
        codec_policy="prores_proxy",
        resolution_policy="half",
        app_version="1",
    )


def _make_entry(
    source_path: str = "/a/b.mov",
    status: ProxyStatus = ProxyStatus.READY,
    proxy_path: str | None = "/cache/proxy/b.mov",
) -> ProxyManifestEntry:
    return ProxyManifestEntry(
        key=_make_key(source_path),
        status=status,
        proxy_path=proxy_path,
        width=1920,
        height=1080,
        codec="prores_ks",
        pixel_format="yuv422p10le",
        created_at=time.time(),
        updated_at=time.time(),
    )


# =============================================================================
# ProxyManifestKey
# =============================================================================


class TestProxyManifestKey:
    def test_to_hash_stable(self) -> None:
        k1 = _make_key()
        k2 = _make_key()
        assert k1.to_hash() == k2.to_hash()
        assert len(k1.to_hash()) == 32

    def test_different_source_different_hash(self) -> None:
        k1 = _make_key("/a.mov")
        k2 = _make_key("/b.mov")
        assert k1.to_hash() != k2.to_hash()

    def test_mtime_change_different_hash(self) -> None:
        """源文件 mtime 变化 → key 变化（失效判断基础）。"""
        k1 = _make_key(mtime=1000)
        k2 = _make_key(mtime=2000)
        assert k1.to_hash() != k2.to_hash()

    def test_frozen(self) -> None:
        k = _make_key()
        with pytest.raises(Exception):
            k.source_path = "/other"  # type: ignore[misc]


# =============================================================================
# ProxyManifestEntry
# =============================================================================


class TestProxyManifestEntry:
    def test_is_ready(self) -> None:
        e = _make_entry(status=ProxyStatus.READY)
        assert e.is_ready is True
        assert e.is_generating is False

    def test_is_generating(self) -> None:
        e = _make_entry(status=ProxyStatus.GENERATING, proxy_path=None)
        assert e.is_generating is True
        assert e.is_ready is False

    def test_serialization_roundtrip(self) -> None:
        e = _make_entry()
        d = e.to_dict()
        e2 = ProxyManifestEntry.from_dict(d)
        assert e2.key == e.key
        assert e2.status is e.status
        assert e2.proxy_path == e.proxy_path

    def test_from_dict_invalid_status_falls_back(self) -> None:
        d = _make_entry().to_dict()
        d["status"] = "bogus"
        e = ProxyManifestEntry.from_dict(d)
        assert e.status is ProxyStatus.MISSING


# =============================================================================
# ProxyManifest (persistence + query)
# =============================================================================


class TestProxyManifest:
    def test_load_empty_when_no_file(self, tmp_path: Path) -> None:
        m = ProxyManifest(tmp_path)
        m.load()
        assert m.entry_count == 0
        assert m.get_entry("/x") is None

    def test_upsert_and_query(self, tmp_path: Path) -> None:
        m = ProxyManifest(tmp_path)
        m.load()
        e = _make_entry("/a.mov")
        m.upsert_entry(e)
        assert m.entry_count == 1
        got = m.get_entry("/a.mov")
        assert got is not None
        assert got.is_ready

    def test_query_is_fast(self, tmp_path: Path) -> None:
        """规格书 §7 验收：查询 <1ms（纯内存）。"""
        m = ProxyManifest(tmp_path)
        m.load()
        for i in range(200):
            m.upsert_entry(_make_entry(f"/a/{i}.mov"))
        import time as _t
        t0 = _t.perf_counter()
        for _ in range(1000):
            m.get_entry("/a/100.mov")
        elapsed_ms = (_t.perf_counter() - t0) * 1000.0
        per_query_us = elapsed_ms / 1000.0 * 1000.0
        # 单次查询应远低于 1ms
        assert per_query_us < 1000.0, f"query too slow: {per_query_us:.1f} us/query"

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        m1 = ProxyManifest(tmp_path)
        m1.load()
        m1.upsert_entry(_make_entry("/a.mov"))
        m1.upsert_entry(_make_entry("/b.mov"))
        m1.save()
        assert (tmp_path / "manifest.json").exists()

        m2 = ProxyManifest(tmp_path)
        m2.load()
        assert m2.entry_count == 2
        assert m2.get_entry("/a.mov") is not None
        assert m2.get_entry("/b.mov") is not None

    def test_invalidate_by_source(self, tmp_path: Path) -> None:
        m = ProxyManifest(tmp_path)
        m.load()
        m.upsert_entry(_make_entry("/a.mov"))
        m.invalidate_by_source("/a.mov", "source modified")
        entry = m.get_entry("/a.mov")
        assert entry is not None
        assert entry.status is ProxyStatus.STALE

    def test_remove_entry(self, tmp_path: Path) -> None:
        m = ProxyManifest(tmp_path)
        m.load()
        m.upsert_entry(_make_entry("/a.mov"))
        m.remove_entry("/a.mov")
        assert m.get_entry("/a.mov") is None
        assert m.entry_count == 0

    def test_has_ready_proxy(self, tmp_path: Path) -> None:
        m = ProxyManifest(tmp_path)
        m.load()
        m.upsert_entry(_make_entry("/a.mov", status=ProxyStatus.READY))
        m.upsert_entry(_make_entry("/b.mov", status=ProxyStatus.GENERATING, proxy_path=None))
        assert m.has_ready_proxy("/a.mov") is True
        assert m.has_ready_proxy("/b.mov") is False
        assert m.has_ready_proxy("/missing.mov") is False

    def test_stats(self, tmp_path: Path) -> None:
        m = ProxyManifest(tmp_path)
        m.load()
        m.upsert_entry(_make_entry("/a.mov", status=ProxyStatus.READY))
        m.upsert_entry(_make_entry("/b.mov", status=ProxyStatus.GENERATING, proxy_path=None))
        s = m.stats()
        assert s["total"] == 2
        assert s.get("ready", 0) == 1
        assert s.get("generating", 0) == 1

    def test_load_skips_malformed_entries(self, tmp_path: Path) -> None:
        """损坏的 entry 不应让整个 manifest 加载失败。"""
        import json
        bad_data = {
            "schema_version": 1,
            "entries": [
                {"bogus": "no key"},
                _make_entry("/ok.mov").to_dict(),
            ],
        }
        (tmp_path / "manifest.json").write_text(json.dumps(bad_data), encoding="utf-8")
        m = ProxyManifest(tmp_path)
        m.load()
        assert m.get_entry("/ok.mov") is not None
        assert m.entry_count == 1


# =============================================================================
# build_manifest_key
# =============================================================================


class TestBuildManifestKey:
    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        key = build_manifest_key(str(tmp_path / "nope.mov"), DiskProxyConfig())
        assert key is None

    def test_captures_file_info(self, tmp_path: Path) -> None:
        f = tmp_path / "v.mov"
        f.write_bytes(b"x" * 100)
        key = build_manifest_key(str(f), DiskProxyConfig(), app_version="2")
        assert key is not None
        assert key.source_size == 100
        assert key.app_version == "2"

    def test_mtime_change_changes_key(self, tmp_path: Path) -> None:
        f = tmp_path / "v.mov"
        f.write_bytes(b"x" * 100)
        k1 = build_manifest_key(str(f), DiskProxyConfig())
        os.utime(str(f), (time.time() + 100, time.time() + 100))
        k2 = build_manifest_key(str(f), DiskProxyConfig())
        assert k1 != k2


# =============================================================================
# AssetProxyManager
# =============================================================================


@pytest.fixture
def real_video(tmp_path: Path) -> Path:
    """创建一个临时源文件用于 manifest key 计算。"""
    f = tmp_path / "source.mov"
    f.write_bytes(b"\x00" * 50)
    return f


@pytest.fixture
def manager(tmp_path: Path, real_video: Path) -> AssetProxyManager:
    mgr = AssetProxyManager()
    mgr.configure(cache_dir=tmp_path / "proxy_cache", app_version="1")
    return mgr


class TestAssetProxyManagerQuery:
    def test_get_proxy_entry_none_when_unconfigured(self) -> None:
        mgr = AssetProxyManager()
        # 未 configure
        assert mgr.get_proxy_entry("/x") is None

    def test_get_proxy_entry_empty_initially(self, manager: AssetProxyManager) -> None:
        assert manager.get_proxy_entry("/nope") is None

    def test_is_proxy_ready_false_initially(self, manager: AssetProxyManager) -> None:
        assert manager.is_proxy_ready("/nope") is False


class TestAssetProxyManagerEnsureDedup:
    """规格书 §7.3 核心验收：同源只生成一次。"""

    def test_same_source_ensure_deduped(
        self, manager: AssetProxyManager, real_video: Path
    ) -> None:
        """同一 source_path 多次 ensure_proxy_async 只触发一次生成。"""
        mock_service = MagicMock()
        mock_service.generate_proxy.return_value = "task-1"
        manager._disk_proxy_service = mock_service
        manager._register_disk_service_callback()

        # 连续 ensure 3 次
        e1 = manager.ensure_proxy_async(source_path=str(real_video), asset_id="a1")
        e2 = manager.ensure_proxy_async(source_path=str(real_video), asset_id="a2")
        e3 = manager.ensure_proxy_async(source_path=str(real_video), asset_id="a3")

        # 都返回 GENERATING
        assert e1.status is ProxyStatus.GENERATING
        assert e2.status is ProxyStatus.GENERATING
        assert e3.status is ProxyStatus.GENERATING
        # generate_proxy 只被调用一次（去重）
        assert mock_service.generate_proxy.call_count == 1

    def test_different_sources_each_generate(
        self, manager: AssetProxyManager, tmp_path: Path
    ) -> None:
        f1 = tmp_path / "a.mov"; f1.write_bytes(b"a")
        f2 = tmp_path / "b.mov"; f2.write_bytes(b"b")
        mock_service = MagicMock()
        mock_service.generate_proxy.return_value = "task"
        manager._disk_proxy_service = mock_service

        manager.ensure_proxy_async(source_path=str(f1), asset_id="a1")
        manager.ensure_proxy_async(source_path=str(f2), asset_id="a2")
        assert mock_service.generate_proxy.call_count == 2

    def test_ready_proxy_not_re_generated(
        self, manager: AssetProxyManager, real_video: Path
    ) -> None:
        """ready 的代理不重新生成。"""
        # 预置 ready entry
        key = build_manifest_key(str(real_video), manager._config, manager._app_version)
        assert key is not None
        ready_entry = ProxyManifestEntry(
            key=key, status=ProxyStatus.READY, proxy_path="/cache/p.mov",
            width=1920, height=1080, codec="prores", pixel_format="yuv422p10le",
            created_at=time.time(), updated_at=time.time(),
        )
        manager._manifest.upsert_entry(ready_entry)

        mock_service = MagicMock()
        manager._disk_proxy_service = mock_service

        e = manager.ensure_proxy_async(source_path=str(real_video), asset_id="a1")
        assert e.is_ready
        assert mock_service.generate_proxy.call_count == 0

    def test_missing_source_returns_failed(
        self, manager: AssetProxyManager
    ) -> None:
        e = manager.ensure_proxy_async(source_path="/no/such.mov", asset_id="a1")
        assert e.status is ProxyStatus.FAILED
        assert e.error is not None


class TestAssetProxyManagerOpenSource:
    def test_open_ready_source(self, manager: AssetProxyManager, real_video: Path) -> None:
        key = build_manifest_key(str(real_video), manager._config, manager._app_version)
        assert key is not None
        entry = ProxyManifestEntry(
            key=key, status=ProxyStatus.READY, proxy_path="/cache/p.mov",
            width=1920, height=1080, codec="prores", pixel_format="yuv422p10le",
            created_at=time.time(), updated_at=time.time(),
        )
        # mock MediaSourceFactory
        fake_source = object()
        with patch("src.shared.media.asset_proxy_manager.MediaSourceFactory", create=True) as _:
            with patch("src.shared.media.factory.MediaSourceFactory") as mock_factory:
                mock_factory.create.return_value = fake_source
                src = manager.open_proxy_source(entry)
        assert src is fake_source

    def test_open_non_ready_returns_none(self, manager: AssetProxyManager) -> None:
        entry = ProxyManifestEntry(
            key=_make_key(), status=ProxyStatus.GENERATING, proxy_path=None,
            width=0, height=0, codec="", pixel_format="",
            created_at=time.time(), updated_at=time.time(),
        )
        assert manager.open_proxy_source(entry) is None
