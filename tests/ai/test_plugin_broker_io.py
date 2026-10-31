"""P15 host-owned plugin I/O capability tests."""

from __future__ import annotations

from typing import Any

from src.features.ai_harness.plugin_broker import (
    AllowlistResourceBroker,
    NetworkGrant,
    PathGrant,
    PluginHostIO,
)
from src.features.ai_harness.plugin_runtime import PluginExecutionPolicy


class _Response:
    status = 200
    headers = {"Content-Length": "4", "Content-Type": "text/plain"}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return b"pong"


class _Opener:
    def open(self, _request: Any, *, timeout: float) -> _Response:
        assert timeout > 0
        return _Response()


def test_host_io_enforces_allowlist_and_file_bounds(tmp_path) -> None:
    plugin_id = "topos.io"
    broker = AllowlistResourceBroker(
        path_grants={plugin_id: (PathGrant(str(tmp_path), allow_read=True, allow_write=True),)},
    )
    io = PluginHostIO(
        broker,
        plugin_id,
        PluginExecutionPolicy(allow_filesystem=True),
        max_bytes=8,
    )
    source = tmp_path / "source.txt"
    source.write_bytes(b"hello")

    read = io.read_bytes(str(source))
    assert read.ok is True
    assert read.data == b"hello"
    written = io.write_bytes(str(tmp_path / "created.txt"), b"world")
    assert written.code == "PLUGIN_IO_WRITE_OK"
    assert (tmp_path / "created.txt").read_bytes() == b"world"

    oversized = io.write_bytes(str(tmp_path / "large.txt"), b"123456789")
    assert oversized.code == "PLUGIN_IO_SIZE_LIMIT"
    outside = io.read_bytes(str(tmp_path.parent / "outside.txt"))
    assert outside.code == "PLUGIN_PATH_NOT_ALLOWLISTED"


def test_host_io_network_is_bounded_and_headers_are_allowlisted() -> None:
    plugin_id = "topos.net"
    broker = AllowlistResourceBroker(
        network_grants={plugin_id: (NetworkGrant("api.example.com"),)},
    )
    io = PluginHostIO(
        broker,
        plugin_id,
        PluginExecutionPolicy(allow_network=True),
        opener=_Opener(),
    )
    response = io.request_bytes(
        "https://api.example.com/ping",
        headers={"Accept": "text/plain"},
    )
    assert response.ok is True
    assert response.data == b"pong"
    assert response.status_code == 200
    denied_header = io.request_bytes(
        "https://api.example.com/ping",
        headers={"Authorization": "Bearer secret"},
    )
    assert denied_header.code == "PLUGIN_HTTP_HEADER_DENIED"
    denied_url = io.request_bytes("https://other.example.com/ping")
    assert denied_url.code == "PLUGIN_NETWORK_NOT_ALLOWLISTED"


def test_default_deny_host_io_never_opens_a_path(tmp_path) -> None:
    from src.features.ai_harness.plugin_broker import DefaultDenyResourceBroker

    io = PluginHostIO(
        DefaultDenyResourceBroker(),
        "topos.denied",
        PluginExecutionPolicy(allow_filesystem=True),
    )
    result = io.read_bytes(str(tmp_path / "missing.txt"))
    assert result.code == "PLUGIN_PATH_DENIED"

