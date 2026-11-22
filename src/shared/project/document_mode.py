"""Infer a runtime document's archive mode without depending on its UI page."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def infer_project_mode(project: Any, *, work_mode: str | None = None) -> str:
    """Keep canonical image documents image-owned in every product launcher.

    An empty Edit document, or one containing only imported stills without an
    image-owned grade, remains an Edit document. Legacy image grades can lack
    source_kind; their asset-default ownership or image workflow disambiguates
    them. No project payload is modified while choosing its archive mode.
    """
    if getattr(project, "timelines", None):
        return "edit_project"
    assets = getattr(project, "assets", None)
    stacks = getattr(project, "color_stacks", None)
    if (not isinstance(assets, Mapping) or not assets
            or any(getattr(asset, "media_type", None) != "image" for asset in assets.values())
            or not isinstance(stacks, Mapping)):
        return "edit_project"
    for stack in stacks.values():
        asset_id = getattr(stack, "asset_id", None)
        if (not isinstance(asset_id, str) or asset_id not in assets
                or getattr(stack, "timeline_id", None)
                or getattr(stack, "clip_id", None)):
            continue
        source_kind = getattr(stack, "source_kind", None)
        if source_kind in ("standalone_image", "single_asset"):
            return "standalone_image"
        legacy_source = source_kind in (None, "", "timeline_clip", "project_asset")
        if legacy_source and getattr(stack, "scope", None) == "asset_default":
            return "standalone_image"
        if legacy_source and work_mode == "image" and len(assets) == 1:
            return "standalone_image"
    return "edit_project"
