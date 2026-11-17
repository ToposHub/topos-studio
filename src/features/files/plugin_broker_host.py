"""F9：Files 宿主的实际资源 broker——授权执行/配置命名空间/secret 代理。

共享契约（``src/shared/plugins/broker.py``：``BrokerDecision``/``PathGrant``/
``NetworkGrant``）此前只有 AI harness 的具体实现——Files 插件的文件/网络/
配置/secret 没有强制执行边界。本模块的口径：

* **声明权限 ≠ 已强制执行**：manifest 声明只是**额外门**（policy 参数），
  授权只来自宿主签发的 grant——default deny（共享契约原文）。声明了
  ``network`` 而没有 ``NetworkGrant`` 的插件照样被拒；
* **路径 grant**：按真实文件系统根解析（``resolve`` 防 ``..``/符号逃逸），
  read/write 分权；**库切换撤授权**——库作用域 grant 绑定签发时的库纪元
  （epoch），``on_library_switched`` 推进纪元后旧 grant 全部失效（新库
  需重新授权，不继承）；
* **网络 grant**：domain+scheme 精确匹配（子域需显式 opt-in），URL 携带
  userinfo 一律拒绝；
* **配置命名空间**：``PluginConfigStore`` 物理按 ``plugin_id`` 分区，
  键逃逸（``../``、绝对键）拒绝——插件只能动自己的命名空间；
* **secret 代理**：明文只在宿主侧（``SecretProxy.register``），插件按名
  引用；解析时要求该插件被 grant 了这个 secret，且结果交给**宿主执行层**
  （如带凭据的网络请求由宿主发）——secret 永不进入插件上下文；
* broker 只决策不做 I/O（共享契约）；宿主执行层据 ``BrokerDecision`` 行动。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from src.shared.plugins.broker import (
    BrokerDecision,
    NetworkGrant,
    PathGrant,
)


class FilesPluginBroker:
    """Files 插件的文件/网络授权执行（决策与 I/O 分离；default deny）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._path_grants: dict[str, list[tuple[PathGrant, int]]] = {}
        self._network_grants: dict[str, list[NetworkGrant]] = {}
        self._library_epoch = 0

    # ------------------------------------------------------------ 授权管理
    def grant_path(self, plugin_id: str, root: str, *,
                   read: bool = True, write: bool = False,
                   library_scoped: bool = True) -> PathGrant:
        """签发路径 grant；``library_scoped`` 绑定当前库纪元（切库失效）。"""
        grant = PathGrant(root=str(root), allow_read=read, allow_write=write)
        with self._lock:
            self._path_grants.setdefault(plugin_id, []).append(
                (grant, self._library_epoch if library_scoped else -1))
        return grant

    def grant_network(self, plugin_id: str, domain: str, *,
                      schemes: tuple[str, ...] = ("https",),
                      allow_subdomains: bool = False) -> NetworkGrant:
        grant = NetworkGrant(domain=domain, schemes=schemes,
                             allow_subdomains=allow_subdomains)
        with self._lock:
            self._network_grants.setdefault(plugin_id, []).append(grant)
        return grant

    def revoke_all(self, plugin_id: str) -> int:
        """停用/退役撤权：摘除该插件全部 grant；返回摘除数。"""
        with self._lock:
            paths = self._path_grants.pop(plugin_id, [])
            networks = self._network_grants.pop(plugin_id, [])
            return len(paths) + len(networks)

    def on_library_switched(self) -> int:
        """[F9] 库切换撤授权：推进库纪元——库作用域 grant 全部失效。

        返回因此失效的 grant 数；新库的授权须重新签发（不继承旧库）。
        """
        with self._lock:
            stale = 0
            for plugin_id, entries in self._path_grants.items():
                stale += sum(1 for _grant, epoch in entries if epoch >= 0)
            self._path_grants = {
                plugin_id: [entry for entry in entries if entry[1] < 0]
                for plugin_id, entries in self._path_grants.items()}
            self._library_epoch += 1
            return stale

    # ------------------------------------------------------------ 决策
    def check_path(self, plugin_id: str, path: str, *,
                   mode: str = "read", policy: Any = None) -> BrokerDecision:
        if mode not in ("read", "write"):
            return BrokerDecision(False, "BAD_MODE", f"未知访问模式：{mode}", path)
        with self._lock:
            epoch = self._library_epoch
            entries = list(self._path_grants.get(plugin_id, ()))
        if not entries:
            return BrokerDecision(
                False, "NO_GRANT",
                f"{plugin_id} 没有任何路径授权（声明权限不构成授权）", path)
        target = Path(path).expanduser()
        try:
            resolved = target.resolve(strict=False)
        except OSError as exc:
            return BrokerDecision(False, "PATH_UNRESOLVED", str(exc), path)
        for grant, grant_epoch in entries:
            if grant_epoch >= 0 and grant_epoch != epoch:
                continue   # 库切换后旧纪元 grant 失效
            if not (resolved.is_relative_to(Path(grant.resolved_root))):
                continue
            if mode == "read" and not grant.allow_read:
                continue
            if mode == "write" and not grant.allow_write:
                continue
            decision = self._policy_gate(policy, "path", path)
            if decision is not None:
                return decision
            return BrokerDecision(True, "GRANTED", "路径授权通过", path)
        return BrokerDecision(
            False, "PATH_OUT_OF_GRANT",
            "路径不在任何有效授权根内（或读写权限不符/库已切换）", path)

    def check_network(self, plugin_id: str, url: str, *,
                      policy: Any = None) -> BrokerDecision:
        with self._lock:
            grants = list(self._network_grants.get(plugin_id, ()))
        if not grants:
            return BrokerDecision(
                False, "NO_GRANT",
                f"{plugin_id} 没有任何网络授权（声明权限不构成授权）", url)
        try:
            parts = urlsplit(url.strip())
        except ValueError as exc:
            return BrokerDecision(False, "URL_INVALID", str(exc), url)
        if parts.username or parts.password:
            return BrokerDecision(
                False, "URL_USERINFO", "URL 不得携带用户凭据（用 secret 代理）", url)
        host = (parts.hostname or "").lower().rstrip(".")
        scheme = (parts.scheme or "").lower()
        if not host or not scheme:
            return BrokerDecision(False, "URL_INVALID", "URL 缺少 host/scheme", url)
        for grant in grants:
            if scheme not in grant.schemes:
                continue
            if host == grant.domain or (grant.allow_subdomains
                                        and host.endswith("." + grant.domain)):
                decision = self._policy_gate(policy, "network", url)
                if decision is not None:
                    return decision
                return BrokerDecision(True, "GRANTED", "网络授权通过", url)
        return BrokerDecision(
            False, "NETWORK_OUT_OF_GRANT",
            "目标域名/scheme 不在授权内（子域需显式授权）", url)

    @staticmethod
    def _policy_gate(policy: Any, kind: str, target: str) -> BrokerDecision | None:
        """声明权限 = 额外门：policy 给出且不含该类能力 → 拒绝。"""
        if policy is None:
            return None
        declared = getattr(policy, "declared_permissions", None)
        if declared is None:
            return None
        if kind in {str(item) for item in declared}:
            return None
        return BrokerDecision(
            False, "POLICY_DENIED",
            f"manifest 未声明 {kind} 能力（声明是额外门，不构成授权）", target)


class PluginConfigStore:
    """配置命名空间：按 plugin_id 物理分区；键逃逸拒绝。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._path)

    @staticmethod
    def _validate(plugin_id: str, key: str) -> str:
        key = str(key)
        if (not key or key.startswith("/") or ".." in key.split("/")
                or key.startswith("\\\\")):
            raise ValueError(f"配置键逃逸命名空间：{plugin_id}:{key}")
        return key

    def get(self, plugin_id: str, key: str, default: Any = None) -> Any:
        self._validate(plugin_id, key)
        with self._lock:
            return self._load().get(plugin_id, {}).get(key, default)

    def set(self, plugin_id: str, key: str, value: Any) -> None:
        key = self._validate(plugin_id, key)
        with self._lock:
            data = self._load()
            data.setdefault(plugin_id, {})[key] = value
            self._save(data)

    def namespace(self, plugin_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._load().get(plugin_id, {}))

    def clear_namespace(self, plugin_id: str) -> int:
        """卸载清数据：只清该插件命名空间（代码/数据/凭据分开）。"""
        with self._lock:
            data = self._load()
            removed = len(data.pop(plugin_id, {}))
            if removed:
                self._save(data)
            return removed


class SecretProxy:
    """secret 代理：明文只在宿主侧；插件按名引用，宿主执行层解析。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._secrets: dict[str, str] = {}
        self._grants: dict[str, set[str]] = {}   # plugin_id -> secret names

    def register(self, name: str, value: str) -> None:
        with self._lock:
            self._secrets[str(name)] = str(value)

    def grant(self, plugin_id: str, name: str) -> None:
        with self._lock:
            self._grants.setdefault(plugin_id, set()).add(str(name))

    def revoke_all(self, plugin_id: str) -> int:
        with self._lock:
            return len(self._grants.pop(plugin_id, set()))

    def resolve_for_host(self, plugin_id: str, name: str) -> str | None:
        """宿主执行层取用（带凭据的 I/O 由宿主发起）；插件拿不到明文。

        未授权（未 grant）返回 None——引用不等于授权。
        """
        with self._lock:
            if name not in self._grants.get(plugin_id, set()):
                return None
            return self._secrets.get(name)

    def secret_count(self) -> int:
        with self._lock:
            return len(self._secrets)


__all__ = ["FilesPluginBroker", "PluginConfigStore", "SecretProxy"]
