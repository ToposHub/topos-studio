"""F9：Files 实际 broker——授权执行/配置命名空间/secret 代理/切库撤权。

核心口径：**声明权限不等于已强制执行**（default deny；manifest 声明只是
额外门）；库切换撤授权（库作用域 grant 绑定纪元）；secret 明文只在
宿主侧。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.features.files.plugin_broker_host import (
    FilesPluginBroker, PluginConfigStore, SecretProxy,
)


# ---------------------------------------------------------------------------
# 声明权限 ≠ 授权：default deny + policy 只是额外门
# ---------------------------------------------------------------------------

def test_no_grant_is_denied_even_when_manifest_declares_permission():
    broker = FilesPluginBroker()
    declared = SimpleNamespace(declared_permissions=("network", "path"))
    network = broker.check_network("files.a", "https://api.example.com",
                                   policy=declared)
    assert not network.allowed and network.code == "NO_GRANT"
    path = broker.check_path("files.a", "/media/x.mov", policy=declared)
    assert not path.allowed and path.code == "NO_GRANT"


def test_policy_is_extra_gate_grant_without_declaration_denied():
    broker = FilesPluginBroker()
    broker.grant_network("files.a", "api.example.com")
    silent = SimpleNamespace(declared_permissions=())   # manifest 没声明 network
    decision = broker.check_network("files.a", "https://api.example.com",
                                    policy=silent)
    assert not decision.allowed and decision.code == "POLICY_DENIED"
    # manifest 声明了 network 且有 grant → 通过（声明=门，grant=授权）
    declared = SimpleNamespace(declared_permissions=("network",))
    assert broker.check_network("files.a", "https://api.example.com",
                                policy=declared).allowed


# ---------------------------------------------------------------------------
# 路径 grant：真实根解析、逃逸拒绝、读写分权
# ---------------------------------------------------------------------------

def test_path_grant_containment_and_escape(tmp_path):
    broker = FilesPluginBroker()
    broker.grant_path("files.a", str(tmp_path), read=True, write=False)
    inside = broker.check_path("files.a", str(tmp_path / "media" / "a.mov"))
    assert inside.allowed and inside.code == "GRANTED"
    # .. 逃逸：解析后落在授权根外
    escape = broker.check_path("files.a", str(tmp_path / ".." / "outside.mov"))
    assert not escape.allowed and escape.code == "PATH_OUT_OF_GRANT"
    # 只读 grant 拒绝写
    write = broker.check_path("files.a", str(tmp_path / "a.mov"), mode="write")
    assert not write.allowed
    # 根外路径
    outside = broker.check_path("files.a", "/etc/passwd")
    assert not outside.allowed


def test_write_grant_allows_write_mode(tmp_path):
    broker = FilesPluginBroker()
    broker.grant_path("files.a", str(tmp_path), read=True, write=True)
    assert broker.check_path("files.a", str(tmp_path / "out.mov"),
                             mode="write").allowed
    assert broker.check_path("files.a", str(tmp_path / "b.mov"),
                             mode="bogus").code == "BAD_MODE"


# ---------------------------------------------------------------------------
# 网络 grant：域/scheme 精确、子域 opt-in、userinfo 拒绝
# ---------------------------------------------------------------------------

def test_network_grant_exact_domain_and_subdomain_optin():
    broker = FilesPluginBroker()
    broker.grant_network("files.a", "api.example.com")
    assert broker.check_network("files.a", "https://api.example.com/v1").allowed
    assert not broker.check_network(
        "files.a", "https://cdn.example.com").allowed      # 非子域授权
    assert not broker.check_network(
        "files.a", "http://api.example.com").allowed       # scheme 不符
    broker.grant_network("files.a", "example.com",
                         schemes=("https",), allow_subdomains=True)
    assert broker.check_network("files.a", "https://cdn.example.com").allowed


def test_network_url_userinfo_rejected():
    broker = FilesPluginBroker()
    broker.grant_network("files.a", "api.example.com")
    decision = broker.check_network(
        "files.a", "https://user:pw@api.example.com/v1")
    assert not decision.allowed and decision.code == "URL_USERINFO"


# ---------------------------------------------------------------------------
# 库切换撤授权：纪元失效、非库作用域保留、重签发恢复
# ---------------------------------------------------------------------------

def test_library_switch_revokes_library_scoped_grants(tmp_path):
    broker = FilesPluginBroker()
    broker.grant_path("files.a", str(tmp_path))                    # 库作用域
    broker.grant_path("files.b", str(tmp_path), library_scoped=False)
    stale = broker.on_library_switched()
    assert stale == 1                                             # 只撤库作用域
    assert not broker.check_path("files.a", str(tmp_path / "a.mov")).allowed
    stale_decision = broker.check_path("files.a", str(tmp_path / "a.mov"))
    assert stale_decision.code in ("PATH_OUT_OF_GRANT", "NO_GRANT")
    # 全局作用域不受切库影响
    assert broker.check_path("files.b", str(tmp_path / "b.mov")).allowed
    # 新库重新授权后恢复
    broker.grant_path("files.a", str(tmp_path))
    assert broker.check_path("files.a", str(tmp_path / "a.mov")).allowed


def test_revoke_all_on_deactivation(tmp_path):
    broker = FilesPluginBroker()
    broker.grant_path("files.a", str(tmp_path))
    broker.grant_network("files.a", "api.example.com")
    assert broker.revoke_all("files.a") == 2
    assert broker.check_path("files.a", str(tmp_path / "x")).code == "NO_GRANT"
    assert broker.check_network("files.a", "https://api.example.com").code == "NO_GRANT"
    assert broker.revoke_all("files.a") == 0                      # 幂等


# ---------------------------------------------------------------------------
# 配置命名空间：分区隔离、键逃逸拒绝、卸载清数据
# ---------------------------------------------------------------------------

def test_config_namespaces_are_isolated(tmp_path):
    store = PluginConfigStore(tmp_path / "plugin_config.json")
    store.set("files.a", "theme", "dark")
    store.set("files.b", "theme", "light")
    assert store.get("files.a", "theme") == "dark"
    assert store.get("files.b", "theme") == "light"
    assert store.namespace("files.a") == {"theme": "dark"}
    # A 拿不到 B 的键；未设置的键回默认
    assert store.get("files.a", "missing", 7) == 7


def test_config_key_escape_rejected(tmp_path):
    store = PluginConfigStore(tmp_path / "cfg.json")
    with pytest.raises(ValueError):
        store.set("files.a", "../files.b/pwned", 1)
    with pytest.raises(ValueError):
        store.set("files.a", "/absolute", 1)
    with pytest.raises(ValueError):
        store.get("files.a", "a/../../escape")


def test_config_clear_namespace_only_own_data(tmp_path):
    store = PluginConfigStore(tmp_path / "cfg.json")
    store.set("files.a", "k", 1)
    store.set("files.b", "k", 2)
    assert store.clear_namespace("files.a") == 1
    assert store.get("files.a", "k") is None
    assert store.get("files.b", "k") == 2                    # 他人数据不动


# ---------------------------------------------------------------------------
# secret 代理：明文只在宿主侧；引用不等于授权
# ---------------------------------------------------------------------------

def test_secret_proxy_grant_gated_resolution():
    proxy = SecretProxy()
    proxy.register("api_token", "SECRET-VALUE")
    proxy.grant("files.a", "api_token")
    # 宿主执行层可解析（用于宿主发起的带凭据请求）；未授权插件拿到 None
    assert proxy.resolve_for_host("files.a", "api_token") == "SECRET-VALUE"
    assert proxy.resolve_for_host("files.b", "api_token") is None
    assert proxy.resolve_for_host("files.a", "unknown_secret") is None


def test_secret_revocation_on_deactivation():
    proxy = SecretProxy()
    proxy.register("api_token", "SECRET")
    proxy.grant("files.a", "api_token")
    assert proxy.revoke_all("files.a") == 1
    assert proxy.resolve_for_host("files.a", "api_token") is None
    assert proxy.secret_count() == 1       # 明文留存宿主（可再授权），不下发


# ---------------------------------------------------------------------------
# 宿主会话接线：退役撤权 + 清命名空间
# ---------------------------------------------------------------------------

def test_host_session_retire_revokes_grants_and_clears_config(tmp_path):
    from src.shared.plugins.host_session import PluginHostSession

    session = PluginHostSession()
    session.config_store = PluginConfigStore(tmp_path / "cfg.json")
    session.config_store.set("files.a", "k", 1)
    session.broker.grant_path("files.a", str(tmp_path))
    session.secrets.register("token", "V")
    session.secrets.grant("files.a", "token")
    report = session.retire_plugin("files.a")
    assert report["grants_revoked"] == 2                    # 路径 + secret
    assert report["config_entries_cleared"] == 1
    assert session.broker.check_path("files.a", str(tmp_path)).code == "NO_GRANT"
    assert session.secrets.resolve_for_host("files.a", "token") is None
