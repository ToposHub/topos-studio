"""Bootstrap an externally sandboxed plugin worker over inherited IPC."""

from __future__ import annotations

import os
import pickle
from multiprocessing.connection import Connection

from src.features.ai_harness.plugin_isolation import _plugin_worker


def main() -> int:
    raw_ipc_fd = os.environ.get("TOPOS_PLUGIN_IPC_FD", "")
    raw_bootstrap_fd = os.environ.get("TOPOS_PLUGIN_BOOTSTRAP_FD", "")
    if not raw_ipc_fd or not raw_bootstrap_fd:
        return 2
    try:
        connection = Connection(int(raw_ipc_fd))
        bootstrap_connection = Connection(int(raw_bootstrap_fd))
        try:
            bootstrap = bootstrap_connection.recv()
        finally:
            bootstrap_connection.close()
        if not isinstance(bootstrap, dict):
            return 3
        factory = bootstrap["factory"]
        plugin_id = str(bootstrap["plugin_id"])
        policy = bootstrap["policy"]
        _plugin_worker(connection, factory, plugin_id, policy)
        return 0
    except (KeyError, TypeError, ValueError, OSError, EOFError, pickle.PickleError):
        return 4


if __name__ == "__main__":  # pragma: no cover - launched by external sandbox
    raise SystemExit(main())
