"""RD-05 / TM-07: asset providers + audited MotionAssetPack.

The scene references assets by id, never by absolute path.  An ``AssetProvider``
resolves an id to an ``AssetDescriptor`` (kind, hash, license, frame-accurate
seek metadata, colour/alpha profile) and declares which kinds it can decode.
Providers are registered in an ``AssetProviderRegistry`` so a missing provider
or an unresolved asset is a diagnosable error instead of a broken render.

``MotionAssetPack`` is an audited, versioned bundle of motion assets (icons,
shapes, noise/gradient textures, Lottie/Rive clips) with license + content
hash + a degradation policy (fall back to a still/sequence when a clip format
is unsupported).  No-licence assets block final delivery (plan §TM-07).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import mimetypes
from pathlib import Path
from typing import Any, Mapping, Protocol, Iterable


#: Chunk size for streamed sha256 computation (AUD-27301: whole-file
#: ``read_bytes()`` loaded GB-scale video assets into memory and blocked the
#: calling thread for seconds).
_HASH_CHUNK_BYTES: int = 1024 * 1024

#: Upper bound for the per-provider digest cache (path, mtime, size) → sha256.
_DIGEST_CACHE_MAX: int = 256


class AssetKind(str, Enum):
    RASTER = "raster"
    SVG = "svg"
    GIF = "gif"
    IMAGE_SEQUENCE = "image_sequence"
    VIDEO = "video"
    AUDIO = "audio"
    LOTTIE = "lottie"
    RIVE = "rive"
    FONT = "font"


class AssetLicenseStatus(str, Enum):
    OK = "ok"                 # licensed for preview + delivery
    PREVIEW_ONLY = "preview_only"   # usable in preview, blocks delivery
    UNKNOWN = "unknown"       # provenance not established -> blocks delivery
    MISSING = "missing"


@dataclass(frozen=True)
class AssetDescriptor:
    """Provenance + decode metadata for one asset id."""

    asset_id: str
    kind: AssetKind
    sha256: str = ""
    license: AssetLicenseStatus = AssetLicenseStatus.UNKNOWN
    mime_type: str = ""
    provider_id: str = ""
    width: int = 0
    height: int = 0
    duration_frames: int = 0          # 0 for single-frame assets
    frame_count: int = 1              # 1 for stills
    color_profile: str = "sRGB"
    premultiplied_alpha: bool = False
    source_uri: str = ""              # project-relative; never absolute
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")
        if self.source_uri.startswith("/"):
            raise ValueError("asset source_uri must be relative, not absolute")

    @property
    def is_animated(self) -> bool:
        return self.frame_count > 1 or self.kind in {AssetKind.VIDEO, AssetKind.GIF, AssetKind.LOTTIE, AssetKind.RIVE}

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id, "kind": self.kind.value, "sha256": self.sha256,
            "license": self.license.value, "mime_type": self.mime_type,
            "provider_id": self.provider_id, "width": self.width, "height": self.height,
            "duration_frames": self.duration_frames, "frame_count": self.frame_count,
            "color_profile": self.color_profile, "premultiplied_alpha": self.premultiplied_alpha,
            "source_uri": self.source_uri, "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AssetDescriptor":
        def _kind(v: Any) -> AssetKind:
            try:
                return AssetKind(v)
            except ValueError:
                return AssetKind.RASTER

        def _lic(v: Any) -> AssetLicenseStatus:
            try:
                return AssetLicenseStatus(v)
            except ValueError:
                return AssetLicenseStatus.UNKNOWN

        return cls(
            asset_id=str(data.get("asset_id", "")), kind=_kind(data.get("kind", "raster")),
            sha256=str(data.get("sha256", "")), license=_lic(data.get("license", "unknown")),
            mime_type=str(data.get("mime_type", "")), provider_id=str(data.get("provider_id", "")),
            width=int(data.get("width", 0)), height=int(data.get("height", 0)),
            duration_frames=int(data.get("duration_frames", 0)), frame_count=int(data.get("frame_count", 1)),
            color_profile=str(data.get("color_profile", "sRGB")),
            premultiplied_alpha=bool(data.get("premultiplied_alpha", False)),
            source_uri=str(data.get("source_uri", "")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class SeekResult:
    """Outcome of asking a provider to seek a specific frame of an asset."""

    descriptor: AssetDescriptor
    frame: int
    available: bool
    reason: str = ""


class AssetProvider(Protocol):
    """Versioned, capability-declaring resolver for a class of assets."""

    provider_id: str
    supported_kinds: tuple[AssetKind, ...]

    def resolve(self, asset_id: str) -> AssetDescriptor | None: ...

    def can_seek(self, descriptor: AssetDescriptor) -> bool: ...

    def seek(self, descriptor: AssetDescriptor, frame: int) -> SeekResult: ...


@dataclass(frozen=True)
class AssetPreflightIssue:
    asset_id: str
    code: str        # unresolved | no_provider | license_blocks | missing_file | seek_out_of_range
    message: str


class AssetProviderRegistry:
    """Resolves asset ids across registered providers + preflights delivery."""

    def __init__(self) -> None:
        self._providers: dict[str, AssetProvider] = {}
        self._descriptors: dict[str, AssetDescriptor] = {}

    def register_provider(self, provider: AssetProvider) -> None:
        self._providers[provider.provider_id] = provider

    def register_descriptor(self, descriptor: AssetDescriptor) -> None:
        self._descriptors[descriptor.asset_id] = descriptor

    def resolve(self, asset_id: str) -> tuple[AssetDescriptor, AssetProvider] | None:
        descriptor = self._descriptors.get(asset_id)
        if descriptor is not None:
            provider = self._providers.get(descriptor.provider_id)
            if provider is not None:
                return descriptor, provider
            # Fall back to any provider supporting the kind.
            for provider in self._providers.values():
                if descriptor.kind in provider.supported_kinds:
                    return descriptor, provider
            return descriptor, None  # type: ignore[return-value]
        for provider in self._providers.values():
            resolved = provider.resolve(asset_id)
            if resolved is not None:
                self._descriptors[asset_id] = resolved
                return resolved, provider
        return None

    def preflight(
        self,
        asset_ids: tuple[str, ...],
        *,
        for_delivery: bool = False,
        seek_requests: Mapping[str, int] | None = None,
    ) -> tuple[AssetPreflightIssue, ...]:
        issues: list[AssetPreflightIssue] = []
        for asset_id in asset_ids:
            resolved = self.resolve(asset_id)
            if resolved is None:
                issues.append(AssetPreflightIssue(asset_id, "unresolved", "no provider resolves this asset"))
                continue
            descriptor, provider = resolved
            # AUD-27303: a MISSING-licensed descriptor means the backing file
            # could not be found/read.  Report it in preview mode too (as a
            # non-blocking ``missing_file`` diagnosis) so a moved/deleted asset
            # surfaces before rendering instead of as a broken render; delivery
            # additionally keeps the ``license_blocks`` issue.
            if descriptor.license == AssetLicenseStatus.MISSING:
                issues.append(AssetPreflightIssue(
                    asset_id, "missing_file",
                    f"asset file missing/unreadable: {descriptor.source_uri}"))
            if for_delivery and descriptor.license != AssetLicenseStatus.OK:
                issues.append(AssetPreflightIssue(
                    asset_id, "license_blocks",
                    f"license={descriptor.license.value} blocks delivery"))
            if provider is None:
                issues.append(AssetPreflightIssue(asset_id, "no_provider",
                                                  f"no provider for kind {descriptor.kind.value}"))
                continue
            if seek_requests and asset_id in seek_requests:
                frame = int(seek_requests[asset_id])
                result = provider.seek(descriptor, frame)
                if not result.available:
                    issues.append(AssetPreflightIssue(asset_id, "seek_out_of_range", result.reason))
        return tuple(issues)


class LocalAssetProvider:
    """Resolve a scene's project-relative assets inside one explicit root.

    This provider is intentionally metadata-first: it does not decode media or
    create GPU objects.  It supplies the portable descriptor/seek contract to
    the existing SVG/HTML/Composite render lanes, which own decoding on their
    respective worker threads.
    """

    provider_id = "scene-local"

    def __init__(
        self,
        root: str | Path,
        assets: Iterable[Mapping[str, Any]],
    ) -> None:
        self._root = Path(root).resolve()
        self._assets = {str(item.get("asset_id", "")): dict(item) for item in assets}
        # AUD-27301: (resolved path, mtime_ns, size) → sha256.  Repeated
        # resolves (preflight per asset, repeated preflights per session) used
        # to re-read and re-hash the whole file every time.
        self._digest_cache: dict[tuple[str, int, int], str] = {}

    @staticmethod
    def _kind(raw: Any, path: Path) -> AssetKind:
        try:
            return AssetKind(raw)
        except ValueError:
            suffix = path.suffix.lower()
            if suffix in {".svg"}:
                return AssetKind.SVG
            if suffix in {".gif"}:
                return AssetKind.GIF
            if suffix in {".mp4", ".mov", ".webm", ".mkv"}:
                return AssetKind.VIDEO
            if suffix in {".wav", ".mp3", ".m4a", ".aac", ".flac"}:
                return AssetKind.AUDIO
            if suffix in {".ttf", ".otf", ".woff", ".woff2"}:
                return AssetKind.FONT
            return AssetKind.RASTER

    def _path(self, raw_path: str) -> Path | None:
        if not raw_path or Path(raw_path).is_absolute():
            return None
        try:
            candidate = (self._root / raw_path).resolve()
        except OSError:
            # Unresolvable path (broken symlink target, permission on a parent
            # …) — treat as missing (AUD-27302).
            return None
        if candidate != self._root and self._root not in candidate.parents:
            return None
        return candidate if candidate.is_file() else None

    def _file_sha256(self, path: Path) -> str:
        """Streamed sha256 with a (path, mtime, size)-keyed cache (AUD-27301).

        Raises OSError when the file cannot be stat'd or read — the caller
        degrades that to a MISSING descriptor (AUD-27302).
        """
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = self._digest_cache.get(cache_key)
        if cached is not None:
            return cached
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if len(self._digest_cache) >= _DIGEST_CACHE_MAX:
            # Bound memory: drop the oldest entry (dict preserves insertion
            # order).
            self._digest_cache.pop(next(iter(self._digest_cache)))
        self._digest_cache[cache_key] = digest
        return digest

    def resolve(self, asset_id: str) -> AssetDescriptor | None:
        item = self._assets.get(asset_id)
        if item is None:
            return None
        raw_uri = str(item.get("path", item.get("source_uri", "")))
        path = self._path(raw_uri)
        if path is None:
            return AssetDescriptor(
                asset_id=asset_id,
                kind=self._kind(item.get("kind", "raster"), Path(raw_uri or "asset")),
                provider_id=self.provider_id,
                license=AssetLicenseStatus.MISSING,
                source_uri=str(item.get("path", "")),
            )
        kind = self._kind(item.get("kind", "raster"), path)
        metadata = dict(item.get("metadata", {}))
        try:
            license_status = AssetLicenseStatus(item.get("license", "unknown"))
        except ValueError:
            license_status = AssetLicenseStatus.UNKNOWN
        duration = int(metadata.get("duration_frames", item.get("duration_frames", 0)) or 0)
        frame_count = int(metadata.get("frame_count", item.get("frame_count", 1)) or 1)
        if kind in {AssetKind.VIDEO, AssetKind.GIF, AssetKind.LOTTIE, AssetKind.RIVE}:
            frame_count = max(2, frame_count)
        # AUD-27302: a single unreadable file (permissions, deleted between
        # the is_file() check and the read, unreadable mount) must surface as
        # a MISSING-licensed descriptor — preflight reports a structured issue
        # instead of the OSError tearing down the whole preflight pass.
        try:
            digest = self._file_sha256(path)
        except OSError:
            return AssetDescriptor(
                asset_id=asset_id,
                kind=kind,
                provider_id=self.provider_id,
                license=AssetLicenseStatus.MISSING,
                source_uri=str(item.get("path", "")),
                metadata=metadata,
            )
        return AssetDescriptor(
            asset_id=asset_id,
            kind=kind,
            sha256=digest,
            license=license_status,
            mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            provider_id=self.provider_id,
            duration_frames=duration,
            frame_count=frame_count,
            source_uri=str(item.get("path", "")),
            metadata=metadata,
        )

    @property
    def supported_kinds(self) -> tuple[AssetKind, ...]:
        return tuple(AssetKind)

    def can_seek(self, descriptor: AssetDescriptor) -> bool:
        return descriptor.kind in {
            AssetKind.GIF, AssetKind.IMAGE_SEQUENCE, AssetKind.VIDEO,
            AssetKind.LOTTIE, AssetKind.RIVE,
        }

    def seek(self, descriptor: AssetDescriptor, frame: int) -> SeekResult:
        if frame < 0:
            return SeekResult(descriptor, frame, False, "frame must be non-negative")
        if not self.can_seek(descriptor):
            return SeekResult(descriptor, frame, frame == 0, "static asset only supports frame 0")
        if descriptor.duration_frames > 0 and frame >= descriptor.duration_frames:
            return SeekResult(descriptor, frame, False, "frame exceeds asset duration")
        return SeekResult(descriptor, frame, True)


def build_local_asset_provider(scene: Any, root: str | Path) -> LocalAssetProvider:
    """Build the provider from scene asset descriptors without storing paths globally."""
    assets = [asset.to_dict() for asset in getattr(scene, "asset_refs", ())]
    return LocalAssetProvider(root, assets)


# ---------------------------------------------------------------------------
# MotionAssetPack (TM-07)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MotionAssetPack:
    """Audited, versioned bundle of motion assets with a degradation policy."""

    pack_id: str
    version: int
    assets: tuple[AssetDescriptor, ...] = ()
    license: AssetLicenseStatus = AssetLicenseStatus.UNKNOWN
    content_hash: str = ""
    # When a clip format (e.g. Lottie) is unsupported, fall back to this asset id.
    fallback_for: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.pack_id:
            raise ValueError("pack_id cannot be empty")
        if self.version <= 0:
            raise ValueError("pack version must be positive")
        ids = [a.asset_id for a in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("pack asset ids must be unique")

    def asset_ids(self) -> tuple[str, ...]:
        return tuple(a.asset_id for a in self.assets)

    def resolve_fallback(self, asset_id: str) -> str | None:
        return self.fallback_for.get(asset_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id, "version": self.version,
            "license": self.license.value, "content_hash": self.content_hash,
            "assets": [a.to_dict() for a in self.assets],
            "fallback_for": dict(self.fallback_for),
        }


__all__ = [
    "AssetDescriptor", "AssetKind", "AssetLicenseStatus", "AssetPreflightIssue",
    "AssetProvider", "AssetProviderRegistry", "LocalAssetProvider", "MotionAssetPack",
    "SeekResult", "build_local_asset_provider",
]
