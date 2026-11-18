"""Builtin Topos media contributions registered in the native plugin host."""
from __future__ import annotations

from typing import Any
from src.shared.plugins.manifest import FormatProviderDeclaration
from src.shared.plugins.providers import ProviderRegistry


def topos_image_result(provides: str, path: str) -> Any:
    from src.shared.media.topos_image import _decode_rgb, get_topos_image_metadata
    if provides == "thumbnail":
        return _decode_rgb(path, target_size=(600, 600))[0]
    return get_topos_image_metadata(path)


def topos_video_result(provides: str, path: str) -> Any:
    from src.shared.playback.topos_player import ToposMovieSession
    session = ToposMovieSession(path, slice_workers=1)
    try:
        if provides == "thumbnail":
            return session.frame_rgb(0, size=(600, 600))
        return {"Codec": "Topos Video Codec", "Resolution": f"{session.width}x{session.height}",
                "Frame Rate": session.fps, "Duration": session.duration_sec,
                "Bit Depth": int(session.info.bit_depth)}
    finally:
        session.close()


def register_native_media(registry: ProviderRegistry, enabled: set[str]) -> None:
    """Keep two explicit owner-scoped providers aligned with current preferences."""
    for key, formats, executor in (
        ("topos_image", ("toos",), topos_image_result),
        ("video_pro", ("topos",), topos_video_result),
    ):
        owner = f"files.{key}"
        registry.unregister_owner(owner)
        if key in enabled:
            registry.register(owner, FormatProviderDeclaration(
                provider_id=f"{owner}.media", formats=formats,
                provides=("probe", "metadata", "thumbnail"),
                version="1.0.0", cache_signature=f"{owner}:native-v1"), executor)
