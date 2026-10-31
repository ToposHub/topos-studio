"""F6.6 · 代理与缓存安全清理——验收测试。

验收锚点：不能误删原片/归档/其他工程必需文件；活动任务受保护；
孤儿代理可按需清；占用统计一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.media.asset_proxy_manager import AssetProxyManager
from src.shared.media.proxy_manifest import (
    ProxyManifest,
    ProxyManifestEntry,
    ProxyManifestKey,
    ProxyStatus,
)


def _manager_with_entries(tmp_path, entries_spec):
    """构造带 manifest 条目的 manager（proxy 文件真实落盘）。"""
    proxy_dir = tmp_path / "proxy_dir"
    proxy_dir.mkdir()
    manifest = ProxyManifest(cache_dir=proxy_dir)
    for source_name, proxy_name, status, content in entries_spec:
        src = tmp_path / source_name
        src.write_bytes(b"original-media")  # 原片真实存在
        proxy_path = proxy_dir / proxy_name
        proxy_path.write_bytes(content)
        entry = ProxyManifestEntry(
            key=ProxyManifestKey(
                source_path=str(src), source_mtime_ns=1, source_size=16,
                codec_policy="prores_proxy", resolution_policy="half",
                app_version="test"),
            status=status, proxy_path=str(proxy_path),
            width=960, height=540, codec="prores_ks",
            pixel_format="yuv422p10le", created_at=0.0, updated_at=0.0)
        manifest.upsert_entry(entry)
    mgr = AssetProxyManager.__new__(AssetProxyManager)
    mgr._manifest = manifest
    return mgr, proxy_dir, tmp_path


class TestSafePurge:
    def test_purge_removes_only_registered_proxies(self, tmp_path):
        """只删 manifest 登记的代理——同目录外部文件（归档/他工程）不动。"""
        mgr, proxy_dir, _ = _manager_with_entries(tmp_path, [
            ("src1.mov", "p1.proxy.mov", ProxyStatus.READY, b"proxy-1")])
        outside = proxy_dir / "someone-elses-file.mov"
        outside.write_bytes(b"not-registered")
        result = mgr.purge_proxies()
        assert result["removed"] == 1
        assert not (proxy_dir / "p1.proxy.mov").exists()
        assert outside.exists()  # 未登记文件绝不删
        # 原片不动
        assert (tmp_path / "src1.mov").exists()

    def test_generating_entries_protected(self, tmp_path):
        """活动任务受保护：GENERATING 跳过（播放中清理不炸任务）。"""
        mgr, _, _ = _manager_with_entries(tmp_path, [
            ("src.mov", "p.proxy.mov", ProxyStatus.GENERATING, b"busy")])
        result = mgr.purge_proxies(statuses=("READY", "STALE", "FAILED",
                                              "GENERATING"))
        assert result["skipped_active"] == 1
        assert result["removed"] == 0

    def test_path_escape_outside_proxy_dir_refused(self, tmp_path):
        """代理路径逃逸（在代理目录外）→ 拒绝删除。"""
        mgr, proxy_dir, _ = _manager_with_entries(tmp_path, [
            ("src.mov", "p.proxy.mov", ProxyStatus.READY, b"x")])
        # 篡改条目 proxy_path 指向外部重要文件
        entry = next(iter(mgr._manifest._entries.values()))
        victim = tmp_path / "archive-important.mov"
        victim.write_bytes(b"precious")
        entry = ProxyManifestEntry(
            key=entry.key, status=ProxyStatus.READY, proxy_path=str(victim),
            width=960, height=540, codec="prores_ks",
            pixel_format="yuv422p10le", created_at=0.0, updated_at=0.0)
        mgr._manifest.upsert_entry(entry)
        result = mgr.purge_proxies()
        assert result["skipped_outside_dir"] == 1
        assert victim.exists()

    def test_orphan_only_mode_keeps_referenced(self, tmp_path):
        """only_if_source_missing：源在（跨项目引用可能）不动；源失才清。"""
        mgr, _, _ = _manager_with_entries(tmp_path, [
            ("src_alive.mov", "alive.proxy.mov", ProxyStatus.READY, b"a"),
            ("src_gone.mov", "gone.proxy.mov", ProxyStatus.READY, b"b")])
        (tmp_path / "src_gone.mov").unlink()  # 源删除 → 孤儿代理
        result = mgr.purge_proxies(only_if_source_missing=True)
        assert result["removed"] == 1
        assert (tmp_path / "src_alive.mov").exists()

    def test_usage_report_counts(self, tmp_path):
        mgr, _, _ = _manager_with_entries(tmp_path, [
            ("s1.mov", "p1.mov", ProxyStatus.READY, b"12345"),
            ("s2.mov", "p2.mov", ProxyStatus.FAILED, b"")])
        report = mgr.proxy_usage_report()
        assert report["total"] == 2
        assert report["statuses"].get("ready") == 1
        assert report["statuses"].get("failed") == 1
