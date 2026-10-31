"""Backward-compatible re-export — new code should import from
src.app.integrations.file_explorer.adapter instead."""
from src.app.integrations.file_explorer.adapter import FileExplorerAdapter as LibraryAdapter  # noqa: F401

# Re-export module-level objects for legacy imports
from src.app.integrations.file_explorer.adapter import (  # noqa: F401
    PROJECT_ROOT,
    external_path,
    file_explorer_path,
    file_explorer_app,
    find_available_port,
    make_server,
    read_port_config,
    FlaskWorker,
)
