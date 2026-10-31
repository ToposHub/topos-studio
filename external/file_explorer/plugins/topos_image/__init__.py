"""Topos Image (.toos) integration for the AssetDeck file explorer.

The explorer only needs a browser-friendly preview and a small metadata
summary.  The original TPIM/TPIC file remains untouched; decoding happens
through the shared ctypes binding and the generated WebP is stored in the
normal explorer thumbnail cache.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[4])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

NAME = "Topos Image"
VERSION = "1.0.0"
AUTHOR = "Topos Color"
DESCRIPTION = "Topos .toos image preview and metadata provider"

logger = logging.getLogger(__name__)


def setup(context):
    # Keep native imports lazy.  The file explorer must still start when a
    # machine has no Topos codec dylib; only opening a .toos then reports a
    # failed thumbnail instead of disabling the whole plugin host.
    import utils

    from .topos_image_media import generate_topos_image_thumbnail, get_topos_image_metadata

    utils.register_thumbnail_provider([".toos"], generate_topos_image_thumbnail)
    utils.register_metadata_provider([".toos"], get_topos_image_metadata)

