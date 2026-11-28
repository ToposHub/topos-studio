"""Classify paths that should not be opened implicitly at startup."""

from __future__ import annotations

import os
import sys


def is_external_mount_path(path: str) -> bool:
    """Check conventional mount locations without accessing the filesystem."""
    normalized = os.path.normpath(os.path.expanduser(path)).replace("\\", "/")
    if normalized.startswith("//"):
        return True
    if sys.platform == "darwin":
        roots = ("/volumes", "/system/volumes/data/volumes", "/network", "/net")
        normalized = normalized.casefold()
    elif sys.platform.startswith("linux"):
        roots = ("/media", "/run/media", "/mnt", "/net")
    else:
        return False
    return any(normalized == root or normalized.startswith(root + "/") for root in roots)
