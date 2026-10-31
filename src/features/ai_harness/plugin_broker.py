"""Default-deny resource brokers for plugin filesystem and network access.

The broker only returns an authorization decision.  It never opens files or
creates sockets, so the host can keep actual I/O in a separately audited
adapter.  A plugin's declarative policy is an additional gate, not a sandbox.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any, Protocol
import urllib.error
import urllib.request
from urllib.parse import urlparse


def _force_binary_fd(fd: int) -> None:
    """Windows：os.open 默认继承 CRT 文本模式，os.read/write 会做 CRLF
    翻译从而损坏二进制内容；逐 fd 强制 BINARY（POSIX 无此概念）。"""
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(fd, os.O_BINARY)


@dataclass(frozen=True)
class BrokerDecision:
    allowed: bool
    code: str
    message: str
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "message": self.message[:500],
            "target": self.target,
        }


@dataclass(frozen=True)
class HostIOResult:
    """Bounded result returned by a host-owned plugin I/O capability."""

    ok: bool
    code: str
    message: str = ""
    data: bytes = b""
    bytes_count: int = 0
    status_code: int = 0
    content_type: str = ""

    def to_dict(self, *, include_data: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "code": self.code,
            "message": self.message[:500],
            "bytes_count": self.bytes_count,
            "status_code": self.status_code,
            "content_type": self.content_type[:200],
        }
        if include_data:
            payload["data"] = self.data
        return payload


class PluginResourceBroker(Protocol):
    def check_path(
        self,
        plugin_id: str,
        path: str,
        *,
        mode: str = "read",
        policy: Any = None,
    ) -> BrokerDecision:
        ...

    def check_network(
        self,
        plugin_id: str,
        url: str,
        *,
        policy: Any = None,
    ) -> BrokerDecision:
        ...


class PluginHostIO:
    """Host-owned, bounded file/network operations for a plugin.

    A plugin receives this capability explicitly through ``PluginContext``.
    Every operation first asks the bound ``PluginResourceBroker`` and then
    performs the I/O in host code; plugins never receive a raw file handle,
    socket, subprocess, or unrestricted URL opener.
    """

    _ALLOWED_METHODS = frozenset({"GET", "POST"})
    _ALLOWED_HEADERS = frozenset({"accept", "content-type", "user-agent"})

    def __init__(
        self,
        broker: PluginResourceBroker,
        plugin_id: str,
        policy: Any,
        *,
        max_bytes: int = 4 * 1024 * 1024,
        network_timeout_seconds: float = 10.0,
        opener: Any = None,
    ) -> None:
        if max_bytes <= 0 or network_timeout_seconds <= 0:
            raise ValueError("host I/O limits must be positive")
        self._broker = broker
        self._plugin_id = str(plugin_id)
        self._policy = policy
        self._max_bytes = int(max_bytes)
        self._network_timeout = float(network_timeout_seconds)
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    def check_path(self, path: str, *, mode: str = "read") -> BrokerDecision:
        return self._broker.check_path(
            self._plugin_id, path, mode=mode, policy=self._policy,
        )

    def check_network(self, url: str) -> BrokerDecision:
        return self._broker.check_network(
            self._plugin_id, url, policy=self._policy,
        )

    def read_bytes(self, path: str, *, max_bytes: int | None = None) -> HostIOResult:
        decision = self.check_path(path, mode="read")
        if not decision.allowed:
            return _decision_result(decision)
        limit = self._bounded_limit(max_bytes)
        if limit is None:
            return HostIOResult(False, "PLUGIN_IO_LIMIT_INVALID", "read limit is invalid")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = os.open(decision.target, flags)
            _force_binary_fd(descriptor)
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                return HostIOResult(False, "PLUGIN_IO_NOT_REGULAR", "only regular files are allowed")
            size = file_stat.st_size
            if size > limit:
                return HostIOResult(
                    False, "PLUGIN_IO_SIZE_LIMIT", "file exceeds host I/O limit",
                    bytes_count=int(size),
                )
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(descriptor, min(64 * 1024, limit - len(data) + 1))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    return HostIOResult(
                        False, "PLUGIN_IO_SIZE_LIMIT", "file exceeds host I/O limit",
                        bytes_count=len(data),
                    )
            return HostIOResult(True, "PLUGIN_IO_READ_OK", bytes_count=len(data), data=bytes(data))
        except (OSError, ValueError) as exc:
            return HostIOResult(False, "PLUGIN_IO_READ_FAILED", str(exc)[:500])
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def write_bytes(
        self,
        path: str,
        data: bytes,
        *,
        overwrite: bool = False,
        max_bytes: int | None = None,
    ) -> HostIOResult:
        decision = self.check_path(path, mode="write")
        if not decision.allowed:
            return _decision_result(decision)
        limit = self._bounded_limit(max_bytes)
        if limit is None:
            return HostIOResult(False, "PLUGIN_IO_LIMIT_INVALID", "write limit is invalid")
        try:
            payload = bytes(data)
        except (TypeError, ValueError) as exc:
            return HostIOResult(False, "PLUGIN_IO_DATA_INVALID", str(exc)[:500])
        if len(payload) > limit:
            return HostIOResult(
                False, "PLUGIN_IO_SIZE_LIMIT", "write payload exceeds host I/O limit",
                bytes_count=len(payload),
            )
        flags = os.O_WRONLY | os.O_CREAT
        flags |= os.O_TRUNC if overwrite else os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        descriptor = -1
        try:
            descriptor = os.open(decision.target, flags, 0o600)
            _force_binary_fd(descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return HostIOResult(False, "PLUGIN_IO_NOT_REGULAR", "only regular files are allowed")
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
            return HostIOResult(True, "PLUGIN_IO_WRITE_OK", bytes_count=len(payload))
        except FileExistsError:
            return HostIOResult(False, "PLUGIN_IO_EXISTS", "target exists and overwrite is disabled")
        except (OSError, ValueError) as exc:
            return HostIOResult(False, "PLUGIN_IO_WRITE_FAILED", str(exc)[:500])
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def request_bytes(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        timeout_seconds: float | None = None,
    ) -> HostIOResult:
        decision = self.check_network(url)
        if not decision.allowed:
            return _decision_result(decision)
        method = str(method or "GET").upper()
        if method not in self._ALLOWED_METHODS:
            return HostIOResult(False, "PLUGIN_HTTP_METHOD_DENIED", "HTTP method is not allowed")
        limit = self._bounded_limit(max_bytes)
        if limit is None:
            return HostIOResult(False, "PLUGIN_IO_LIMIT_INVALID", "network response limit is invalid")
        try:
            request_body = bytes(body)
        except (TypeError, ValueError) as exc:
            return HostIOResult(False, "PLUGIN_HTTP_BODY_INVALID", str(exc)[:500])
        if len(request_body) > limit:
            return HostIOResult(False, "PLUGIN_IO_SIZE_LIMIT", "request body exceeds host I/O limit")
        safe_headers: dict[str, str] = {"User-Agent": "Topos-PluginHost/1"}
        try:
            for name, value in (headers or {}).items():
                normalized = str(name).strip().lower()
                if normalized not in self._ALLOWED_HEADERS:
                    return HostIOResult(False, "PLUGIN_HTTP_HEADER_DENIED", "HTTP header is not allowed")
                safe_headers[normalized.title()] = str(value)[:500]
        except (AttributeError, TypeError, ValueError):
            return HostIOResult(False, "PLUGIN_HTTP_HEADER_INVALID", "HTTP headers are invalid")
        try:
            timeout = self._network_timeout if timeout_seconds is None else float(timeout_seconds)
            request = urllib.request.Request(
                str(url), data=(request_body or None), headers=safe_headers, method=method,
            )
        except (TypeError, ValueError):
            return HostIOResult(False, "PLUGIN_HTTP_REQUEST_INVALID", "HTTP request is invalid")
        if timeout <= 0:
            return HostIOResult(False, "PLUGIN_HTTP_TIMEOUT_INVALID", "network timeout is invalid")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                declared = response.headers.get("Content-Length", "")
                if declared:
                    try:
                        if int(declared) > limit:
                            return HostIOResult(False, "PLUGIN_IO_SIZE_LIMIT", "response exceeds host I/O limit")
                    except ValueError:
                        pass
                data = response.read(limit + 1)
                if len(data) > limit:
                    return HostIOResult(False, "PLUGIN_IO_SIZE_LIMIT", "response exceeds host I/O limit", bytes_count=len(data))
                return HostIOResult(
                    True,
                    "PLUGIN_HTTP_OK",
                    data=bytes(data),
                    bytes_count=len(data),
                    status_code=int(getattr(response, "status", 200) or 200),
                    content_type=str(response.headers.get("Content-Type", ""))[:200],
                )
        except urllib.error.HTTPError as exc:
            return HostIOResult(False, "PLUGIN_HTTP_ERROR", str(exc)[:500], status_code=int(exc.code or 0))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            return HostIOResult(False, "PLUGIN_HTTP_FAILED", str(exc)[:500])

    def _bounded_limit(self, value: int | None) -> int | None:
        limit = self._max_bytes if value is None else value
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return None
        return min(limit, self._max_bytes)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, code, "redirects are denied", headers, fp)


def _decision_result(decision: BrokerDecision) -> HostIOResult:
    return HostIOResult(False, decision.code, decision.message)


class DefaultDenyResourceBroker:
    """Safe broker used when the host has not explicitly configured grants."""

    def check_path(
        self,
        plugin_id: str,
        path: str,
        *,
        mode: str = "read",
        policy: Any = None,
    ) -> BrokerDecision:
        return BrokerDecision(
            False,
            "PLUGIN_PATH_DENIED",
            "plugin filesystem access is not granted by the host",
            str(path),
        )

    def check_network(
        self,
        plugin_id: str,
        url: str,
        *,
        policy: Any = None,
    ) -> BrokerDecision:
        return BrokerDecision(
            False,
            "PLUGIN_NETWORK_DENIED",
            "plugin network access is not granted by the host",
            str(url),
        )


class PolicyBoundResourceBroker:
    """Bind a shared broker to one plugin identity and execution policy."""

    def __init__(self, delegate: PluginResourceBroker, plugin_id: str, policy: Any) -> None:
        self._delegate = delegate
        self._plugin_id = str(plugin_id)
        self._policy = policy

    def check_path(
        self,
        plugin_id: str,
        path: str,
        *,
        mode: str = "read",
        policy: Any = None,
    ) -> BrokerDecision:
        return self._delegate.check_path(
            self._plugin_id, path, mode=mode, policy=self._policy,
        )

    def check_network(
        self,
        plugin_id: str,
        url: str,
        *,
        policy: Any = None,
    ) -> BrokerDecision:
        return self._delegate.check_network(
            self._plugin_id, url, policy=self._policy,
        )


@dataclass(frozen=True)
class PathGrant:
    root: str
    allow_read: bool = True
    allow_write: bool = False

    @property
    def resolved_root(self) -> str:
        return str(Path(self.root).expanduser().resolve(strict=False))


@dataclass(frozen=True)
class NetworkGrant:
    domain: str
    schemes: tuple[str, ...] = ("https",)
    allow_subdomains: bool = False

    def __post_init__(self) -> None:
        domain = str(self.domain or "").strip().lower().rstrip(".")
        if not domain or "/" in domain or ":" in domain:
            raise ValueError("network grant domain must be a hostname")
        schemes = tuple(dict.fromkeys(
            str(item).strip().lower() for item in self.schemes if str(item).strip()
        ))
        if not schemes:
            raise ValueError("network grant requires at least one scheme")
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "schemes", schemes)


class AllowlistResourceBroker:
    """Per-plugin path/domain allowlist with policy and traversal checks."""

    def __init__(
        self,
        *,
        path_grants: Mapping[str, Iterable[PathGrant]] | None = None,
        network_grants: Mapping[str, Iterable[NetworkGrant]] | None = None,
    ) -> None:
        self._path_grants = {
            str(plugin_id): tuple(grants)
            for plugin_id, grants in (path_grants or {}).items()
        }
        self._network_grants = {
            str(plugin_id): tuple(grants)
            for plugin_id, grants in (network_grants or {}).items()
        }

    def check_path(
        self,
        plugin_id: str,
        path: str,
        *,
        mode: str = "read",
        policy: Any = None,
    ) -> BrokerDecision:
        plugin_id = str(plugin_id)
        mode = str(mode).lower()
        if mode not in {"read", "write"}:
            return BrokerDecision(False, "PLUGIN_PATH_MODE_INVALID", "unsupported path mode", str(path))
        if policy is not None and not bool(getattr(policy, "allow_filesystem", False)):
            return BrokerDecision(False, "PLUGIN_FILESYSTEM_POLICY_DENIED", "plugin policy disallows filesystem access", str(path))
        try:
            resolved = str(Path(path).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, TypeError, ValueError):
            return BrokerDecision(False, "PLUGIN_PATH_INVALID", "path cannot be resolved", str(path))
        for grant in self._path_grants.get(plugin_id, ()):
            root = grant.resolved_root
            try:
                inside = os.path.commonpath((resolved, root)) == root
            except ValueError:
                inside = False
            if not inside:
                continue
            if mode == "read" and grant.allow_read:
                return BrokerDecision(True, "PLUGIN_PATH_ALLOWED", "path read granted", resolved)
            if mode == "write" and grant.allow_write:
                return BrokerDecision(True, "PLUGIN_PATH_ALLOWED", "path write granted", resolved)
        return BrokerDecision(False, "PLUGIN_PATH_NOT_ALLOWLISTED", "path is outside plugin grants", resolved)

    def check_network(
        self,
        plugin_id: str,
        url: str,
        *,
        policy: Any = None,
    ) -> BrokerDecision:
        if policy is not None and not bool(getattr(policy, "allow_network", False)):
            return BrokerDecision(False, "PLUGIN_NETWORK_POLICY_DENIED", "plugin policy disallows network access", str(url))
        try:
            parsed = urlparse(str(url))
            hostname = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            hostname = ""
            parsed = None
        if parsed is None or not hostname or parsed.username or parsed.password:
            return BrokerDecision(False, "PLUGIN_URL_INVALID", "network URL is invalid or contains credentials", str(url))
        for grant in self._network_grants.get(str(plugin_id), ()):
            domain_match = hostname == grant.domain or (
                grant.allow_subdomains and hostname.endswith("." + grant.domain)
            )
            if domain_match and parsed.scheme.lower() in grant.schemes:
                return BrokerDecision(True, "PLUGIN_NETWORK_ALLOWED", "network domain granted", str(url))
        return BrokerDecision(False, "PLUGIN_NETWORK_NOT_ALLOWLISTED", "network domain is not allowlisted", str(url))


__all__ = [
    "AllowlistResourceBroker",
    "BrokerDecision",
    "DefaultDenyResourceBroker",
    "HostIOResult",
    "NetworkGrant",
    "PluginHostIO",
    "PolicyBoundResourceBroker",
    "PathGrant",
    "PluginResourceBroker",
]
