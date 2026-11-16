"""Cross-domain plugin resource broker contracts (PL-06).

Decision/DTO/Protocol definitions shared by every plugin host; the AI harness
keeps the concrete implementations (``AllowlistResourceBroker``,
``PolicyBoundResourceBroker``, ``PluginHostIO``) and re-exports these types so
existing imports keep working.

Semantics preserved from the reviewed AI implementation:
- brokers only *decide*; they never open files or sockets themselves — the
  host performs actual I/O after a decision
- default deny; a declared policy is an extra gate, never a sandbox
- path grants resolve against real filesystem roots; network grants are
  domain+scheme exact (opt-in subdomains), no userinfo in URLs
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


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
    """Decides whether one plugin may touch one path/URL; never performs I/O."""

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


__all__ = ["BrokerDecision", "HostIOResult", "PluginResourceBroker", "PathGrant", "NetworkGrant"]
