"""P1-7 vault 只读客户端测试。

测：
    1. VaultClient 降级（vault 没起 → is_available=False，查询返回空）
    2. 用 Flask test client 起真服务，验证 search/stats 正确
    3. 环境变量 TOPOS_VAULT_URL 生效
    4. 超时不卡
"""

from __future__ import annotations

import os
import pytest


# ---------------------------------------------------------------------- #
# 降级：vault 没起
# ---------------------------------------------------------------------- #

def test_client_unavailable_when_no_service():
    """连一个没人监听的端口 → is_available=False。"""
    from src.shared.vault.client import VaultClient
    c = VaultClient(base_url="http://127.0.0.1:1", timeout=0.5)  # 1 端口没人监听
    assert c.is_available() is False


def test_search_returns_empty_when_unavailable():
    """vault 没起 → search 返回 []（不抛异常）。"""
    from src.shared.vault.client import VaultClient
    c = VaultClient(base_url="http://127.0.0.1:1", timeout=0.5)
    assert c.search("微笑") == []


def test_get_stats_returns_none_when_unavailable():
    """vault 没起 → get_stats 返回 None。"""
    from src.shared.vault.client import VaultClient
    c = VaultClient(base_url="http://127.0.0.1:1", timeout=0.5)
    assert c.get_stats() is None


def test_search_empty_query_returns_empty():
    """空查询 → 直接返回 []（不发请求）。"""
    from src.shared.vault.client import VaultClient
    c = VaultClient(base_url="http://127.0.0.1:1", timeout=0.5)
    assert c.search("") == []
    assert c.search("   ") == []


# ---------------------------------------------------------------------- #
# 真服务（用 vault query_service 的 Flask app）
# ---------------------------------------------------------------------- #

@pytest.fixture
def vault_app(vault_dir, initialized_db, repo, sample_files):
    """起一个 vault 查询服务的 Flask test client（不真的监听端口）。"""
    # 先索引一些素材（带 caption 让 search 能命中）
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.db.faiss_store import clear_store_cache
    clear_store_cache()
    orch = Orchestrator(repo)
    orch.index_file(str(sample_files["image"]))  # stub 会写 caption

    from apps.topos_vault.server.app import create_app
    app = create_app(repo)
    app.testing = True
    return app.test_client()


def test_search_against_real_service(vault_app):
    """用 Flask test client 验证 VaultClient 能查到数据。

    VaultClient 直接调 /api/search HTTP，这里用 test_client 模拟。
    """
    # 先直接测 Flask 端点（stub caption 含 "[stub]"，搜不到则空）
    resp = vault_app.get("/api/search?q=stub&type=image")
    data = resp.get_json()
    assert resp.status_code == 200
    # stub caption 形如 "[stub] 确定性描述（xxxx）"，搜 "stub" 应命中
    assert data["total"] >= 1
    files = data["files"]
    assert files[0]["vault_caption"] is not None


def test_stats_against_real_service(vault_app):
    """stats 端点返回正确统计。"""
    resp = vault_app.get("/api/stats")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["total"] >= 1
    assert "by_type" in data


def test_health_endpoint(vault_app):
    """health 端点返回 ok。"""
    resp = vault_app.get("/api/health")
    data = resp.get_json()
    assert data["status"] == "ok"


def test_vault_client_via_real_app(vault_app, monkeypatch):
    """VaultClient 通过 monkeypatch 走 test_client（不真开端口）。"""
    from src.shared.vault.client import VaultClient

    c = VaultClient(base_url="http://127.0.0.1:1", timeout=0.5)

    # patch _get 方法：直接调 vault_app 的端点
    import json
    def fake_get(path, params=None):
        qs = ""
        if params:
            qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        resp = vault_app.get(path + qs)
        return resp.get_json()

    monkeypatch.setattr(c, "_get", fake_get)
    assert c.is_available() is True
    results = c.search("stub", media_type="image")
    assert len(results) >= 1
    stats = c.get_stats()
    assert stats is not None
    assert stats.total >= 1


# ---------------------------------------------------------------------- #
# 环境变量
# ---------------------------------------------------------------------- #

def test_base_url_from_env(monkeypatch):
    """TOPOS_VAULT_URL 环境变量生效。"""
    monkeypatch.setenv("TOPOS_VAULT_URL", "http://example.com:9999")
    from src.shared.vault.client import VaultClient
    c = VaultClient()
    assert c.base_url == "http://example.com:9999"


def test_base_url_default_when_env_unset(monkeypatch):
    """未设环境变量 → 默认 127.0.0.1:8288。"""
    monkeypatch.delenv("TOPOS_VAULT_URL", raising=False)
    from src.shared.vault.client import VaultClient, DEFAULT_VAULT_URL
    c = VaultClient()
    assert c.base_url == DEFAULT_VAULT_URL == "http://127.0.0.1:8288"
