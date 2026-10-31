"""Media-drop payload helpers (plan §5).

The plan calls for a rich MIME type ``application/x-topos-media-asset``
carrying asset id, path, media_type, duration, width, height — *not* just
a file path, because the media pool may use proxies, asset ids, thumbnail
caches or relink state.

In the real codebase the media pool already drags
``application/x-topos-media-pool-assets`` (a JSON list of asset ids), and
the matching receiver helpers live in ``media_pool_panel.py``.  To avoid
forking a second, parallel MIME channel we:

1. Keep the wire format that is already on the drag source
   (``parse_media_pool_drag_mime`` returns the asset_ids).
2. Introduce a *rich payload* dataclass (:class:`MediaDropPayload`) that
   the Keyframes drop handler builds by resolving each asset id against
   the open :class:`EditProject`.  This keeps the drop target dependency-
   light (it only needs the project's asset lookup, not the full pool UI).
3. Provide :func:`resolve_drop_payloads` as the single bridge from
   ``QMimeData`` → ``List[MediaDropPayload]``.

The plan's literal MIME name is exposed as :data:`MIME_TYPE_TOPOS_MEDIA_ASSET`
for future use / documentation, but the active transport stays the existing
plural media-pool MIME.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from src.shared.media.format_registry import MODEL_3D_EXTENSIONS

logger = logging.getLogger(__name__)

# Plan §5.1 literal MIME name (kept for reference / future switch).
MIME_TYPE_TOPOS_MEDIA_ASSET = "application/x-topos-media-asset"

# The actual transport MIME the media pool emits today (do not rename —
# the drag source in src/features/edit/ui/media_pool_panel.py uses this).
MIME_TYPE_MEDIA_POOL_ASSETS = "application/x-topos-media-pool-assets"
# Singular compatibility MIME used by older/custom media browsers.
MIME_TYPE_MEDIA_POOL_ASSET = "application/x-topos-media-pool-asset"

# Supported media types for layer drops.
MEDIA_TYPE_IMAGE = "image"
MEDIA_TYPE_VIDEO = "video"
MEDIA_TYPE_SEQUENCE = "sequence"
MEDIA_TYPE_AUDIO = "audio"  # not layer-renderable; drops are rejected, not misrouted
# Imported 3D assets are represented by a Model3D source node and are
# rendered through a Scene3D layer root. Keep the short value stable because
# it is also the value returned by the shared media format registry.
MEDIA_TYPE_MODEL3D = "model"
# Friendly aliases for callers that use the domain term rather than the
# project/media-pool vocabulary.
MEDIA_TYPE_MODEL = MEDIA_TYPE_MODEL3D
MEDIA_TYPE_3D = MEDIA_TYPE_MODEL3D

# Node type chosen per media type when creating the source node.
MEDIA_TYPE_TO_NODE_TYPE = {
    MEDIA_TYPE_IMAGE: "image_input",
    MEDIA_TYPE_VIDEO: "video_frame",
    MEDIA_TYPE_SEQUENCE: "image_input",  # sequences use image_input for now
    MEDIA_TYPE_MODEL3D: "model3d",
}

# AUD-5004: audio extensions recognised when classifying by path.  Audio is
# deliberately absent from MEDIA_TYPE_TO_NODE_TYPE — there is no layer source
# node for it, so resolve_drop_payloads rejects audio drops with a log line
# instead of creating an image_input layer pointing at a sound file.
AUDIO_EXTENSIONS = (
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
    ".aiff", ".aif", ".wma",
)


@dataclass
class MediaDropPayload:
    """Resolved description of one asset being dropped onto the layer panel.

    Built by :func:`resolve_drop_payloads` from the drag MIME + the open
    project's asset index.  Carries everything the controller needs to
    create a source node + layer without re-querying the media pool.
    """

    asset_id: str
    path: str
    media_type: str = MEDIA_TYPE_IMAGE
    duration: int = 1
    width: int = 0
    height: int = 0
    # Human-friendly name; falls back to the basename of ``path``.
    name: str = ""
    # Color metadata carried through from the asset probe, if any.
    color_metadata: Optional[dict] = None
    # Extra arbitrary fields the asset may carry (fps, is_hdr, ...).
    extras: dict = field(default_factory=dict)

    @property
    def node_type(self) -> str:
        """The source node type to instantiate for this media type.

        Raises:
            ValueError: for media types with no layer source node (audio —
                AUD-5004).  ``resolve_drop_payloads`` filters those before
                payloads reach layer creation; the raise keeps any direct
                constructor honest instead of silently building a broken
                image_input layer pointing at an audio file.
        """
        if self.media_type == MEDIA_TYPE_AUDIO:
            raise ValueError(
                "audio assets are not layer-renderable; refusing to map "
                f"asset {self.asset_id!r} to an image_input node")
        return MEDIA_TYPE_TO_NODE_TYPE.get(self.media_type, "image_input")

    @property
    def display_name(self) -> str:
        """Layer-friendly name: explicit name → basename → asset id."""
        if self.name:
            return self.name
        if self.path:
            import os
            return os.path.splitext(os.path.basename(self.path))[0]
        return self.asset_id


def parse_media_pool_mime(mime_data: Any) -> List[str]:
    """Return the asset_ids carried by a media-pool drag, or [].

    Tolerates the custom MIME, a text/plain fallback, or a uri-list
    fallback (paths only — returns []  because ids are not derivable from
    paths alone here; the caller should fall back to file-drop handling).
    """
    if mime_data is None:
        return []
    # 1. Custom MIME (authoritative).  Accept both the active media-pool
    # list transport and the literal singular MIME from the implementation
    # plan so third-party drag sources do not silently become no-ops.
    for mime_type in (MIME_TYPE_MEDIA_POOL_ASSETS,
                      MIME_TYPE_TOPOS_MEDIA_ASSET,
                      MIME_TYPE_MEDIA_POOL_ASSET):
        raw = None
        try:
            qba = mime_data.data(mime_type)
            size = qba.size() if callable(getattr(qba, "size", None)) else len(qba or b"")
            if qba is not None and size > 0:
                raw = bytes(qba).decode("utf-8", errors="replace")
        except Exception:
            raw = None
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            ids = []
            for item in parsed:
                value = item.get("asset_id") if isinstance(item, dict) else item
                if value:
                    value = str(value)
                    if value not in ids:
                        ids.append(value)
            if ids:
                return ids
    # 2. text/plain fallback (comma-separated ids).
    text = ""
    try:
        text = mime_data.text() or ""
    except Exception:
        text = ""
    if text and "," in text:
        # Heuristic: only treat as ids if every token is non-empty and the
        # text doesn't look like a single path.
        tokens = [t.strip() for t in text.split(",") if t.strip()]
        if tokens and not any("/" in t or "\\" in t for t in tokens):
            return list(dict.fromkeys(tokens))
    return []


def resolve_drop_payloads(mime_data: Any,
                          project: Any = None,
                          asset_lookup: Any = None) -> List[MediaDropPayload]:
    """Bridge QMimeData → :class:`MediaDropPayload` list.

    Args:
        mime_data: the drag's ``QMimeData``.
        project: optional :class:`EditProject`-like object exposing
            ``assets`` (a collection of asset objects with ``asset_id``,
            ``path``, ``media_type``, ...).  When None, only path-based
            payloads can be produced.
        asset_lookup: optional callable ``(asset_id) -> asset-or-dict``
            overriding ``project``.  Useful for tests.

    The resolver first asks the custom MIME for asset ids, then resolves
    each id to a payload via the lookup.  When neither ``project`` nor
    ``asset_lookup`` can resolve an id, the id is skipped with a warning.
    """
    asset_ids = parse_media_pool_mime(mime_data)
    fallback_paths = _local_paths_from_mime(mime_data)
    if not asset_ids:
        # A file URL is the last-resort transport when the media-pool
        # project binding is temporarily unavailable. It still creates a
        # valid source node, instead of making the drag appear to do nothing.
        payloads = [
            MediaDropPayload(
                asset_id=f"path:{path}",
                path=path,
                media_type=_classify_media_type(None, path),
                name="",
            )
            for path in fallback_paths
        ]
        return _reject_audio_payloads(payloads)

    payloads: List[MediaDropPayload] = []
    for index, aid in enumerate(asset_ids):
        asset = None
        if asset_lookup is not None:
            try:
                asset = asset_lookup(aid)
            except Exception:
                asset = None
        elif project is not None:
            asset = _lookup_asset_in_project(project, aid)
        if asset is None:
            # MediaPool includes local URLs alongside the authoritative asset
            # ids. Pairing them by order keeps drag/drop usable even when a
            # panel was opened before its EditProject reference was rebound.
            if index < len(fallback_paths):
                path = fallback_paths[index]
                payloads.append(MediaDropPayload(
                    asset_id=aid,
                    path=path,
                    media_type=_classify_media_type(None, path),
                ))
                continue
            logger.warning("Media drop: asset %r not found; skipped.", aid)
            continue
        payloads.append(_payload_from_asset(aid, asset))
    return _reject_audio_payloads(payloads)


def _reject_audio_payloads(
        payloads: List[MediaDropPayload]) -> List[MediaDropPayload]:
    """Filter audio payloads out of a layer-drop list (AUD-5004).

    Audio is not layer-renderable; letting it through made the drop handler
    create an ``image_input`` layer pointing at a sound file — a layer that
    "succeeded" but can never decode.  Rejected assets are logged so the
    drop is diagnosable instead of silently doing nothing.
    """
    kept: List[MediaDropPayload] = []
    for payload in payloads:
        if payload.media_type == MEDIA_TYPE_AUDIO:
            logger.warning(
                "Media drop: audio asset %r (%s) is not layer-renderable; "
                "no layer was created.", payload.asset_id, payload.path)
            continue
        kept.append(payload)
    return kept


def _local_paths_from_mime(mime_data: Any) -> List[str]:
    """Read local file URLs without importing Qt into this module."""
    if mime_data is None or not hasattr(mime_data, "urls"):
        return []
    try:
        urls = mime_data.urls() or []
    except Exception:
        return []
    paths: List[str] = []
    for url in urls:
        try:
            if hasattr(url, "isLocalFile") and not url.isLocalFile():
                continue
            path = url.toLocalFile() if hasattr(url, "toLocalFile") else str(url)
        except Exception:
            continue
        if path:
            paths.append(str(path))
    return list(dict.fromkeys(paths))


def _lookup_asset_in_project(project: Any, asset_id: str) -> Any:
    """Find an asset by id on a project-like object.

    Tolerates projects whose ``assets`` is a dict, list, or has a
    ``get(id)`` accessor.
    """
    assets = getattr(project, "assets", None)
    if assets is None:
        return None
    if isinstance(assets, dict):
        return assets.get(asset_id)
    if hasattr(assets, "get"):
        try:
            return assets.get(asset_id)
        except Exception:
            pass
    if isinstance(assets, (list, tuple)):
        for a in assets:
            if _get(a, "asset_id") == asset_id or _get(a, "id") == asset_id:
                return a
    return None


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict or attribute, or return ``default``."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _payload_from_asset(asset_id: str, asset: Any) -> MediaDropPayload:
    """Build a :class:`MediaDropPayload` from an asset object/dict."""
    path = _get(asset, "path", "") or _get(asset, "file_path", "") or ""
    media_type = _classify_media_type(_get(asset, "media_type"), path)
    raw_duration = _get(asset, "duration")
    if raw_duration is None:
        raw_duration = _get(asset, "total_frames")
    duration = _coerce_int(raw_duration, default=1)
    if duration <= 0:
        # Video/model duration is in frames; image/sequence is a single
        # frame. A model with no probe metadata still gets a usable one-frame
        # source marker; the layer itself uses the composition range.
        duration = 1 if media_type in (MEDIA_TYPE_IMAGE, MEDIA_TYPE_MODEL3D) else 0
    width = _coerce_int(_get(asset, "width"), default=0)
    height = _coerce_int(_get(asset, "height"), default=0)
    name = (_get(asset, "name", "")
            or _get(asset, "display_name", "")
            or _get(asset, "label", ""))
    color_md = _get(asset, "color_metadata")
    extras = {}
    for k in ("fps", "is_hdr", "proxy_path", "thumbnail_path"):
        v = _get(asset, k)
        if v is not None:
            extras[k] = v
    return MediaDropPayload(
        asset_id=asset_id,
        path=path,
        media_type=media_type,
        duration=duration,
        width=width,
        height=height,
        name=name,
        color_metadata=color_md if isinstance(color_md, dict) else None,
        extras=extras,
    )


def _classify_media_type(value: Optional[str], path: str) -> str:
    """Normalise an asset's media_type, guessing from the path if missing."""
    if value:
        v = str(value).lower()
        if v in MEDIA_TYPE_TO_NODE_TYPE:
            return v
        if v in ("img", "still", "picture"):
            return MEDIA_TYPE_IMAGE
        if v in ("clip", "movie", "footage"):
            return MEDIA_TYPE_VIDEO
        if v in ("imgseq", "image_sequence", "frame_sequence"):
            return MEDIA_TYPE_SEQUENCE
        if v in ("model", "model3d", "3d", "3d_model", "mesh"):
            return MEDIA_TYPE_MODEL3D
        # AUD-5004: audio aliases must classify as audio — the previous
        # fall-through re-labelled them "image" and layer creation built a
        # broken image_input node pointing at the sound file.
        if v in ("audio", "sound", "music", "snd"):
            return MEDIA_TYPE_AUDIO
    # Fallback: classify by extension.
    p = (path or "").lower()
    img_exts = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".bmp",
                ".webp", ".dpx", ".hdr")
    vid_exts = (".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm", ".mxf")
    if any(p.endswith(ext) for ext in MODEL_3D_EXTENSIONS):
        return MEDIA_TYPE_MODEL3D
    if p.endswith(vid_exts):
        return MEDIA_TYPE_VIDEO
    if p.endswith(AUDIO_EXTENSIONS):
        return MEDIA_TYPE_AUDIO
    if p.endswith(img_exts):
        return MEDIA_TYPE_IMAGE
    return MEDIA_TYPE_IMAGE  # safe default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
