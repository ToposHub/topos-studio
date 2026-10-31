"""Deterministic, traversal-safe scene asset resolution and preflight."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import mimetypes
from pathlib import Path

from ..core.contracts import RenderContext
from ..core.model import SceneAssetRef, TeachingSceneDocument
from ..core.audio_plan import audio_plan_for_scene


@dataclass(frozen=True)
class AssetIssue:
    asset_id: str
    kind: str
    message: str
    blocking: bool


@dataclass(frozen=True)
class AssetPreflightResult:
    resolved: dict[str, Path]
    issues: tuple[AssetIssue, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not any(issue.blocking for issue in self.issues)


class AssetPreflightError(ValueError):
    """Raised when strict rendering cannot resolve a referenced asset."""

    def __init__(self, result: AssetPreflightResult) -> None:
        self.result = result
        message = "; ".join(issue.message for issue in result.issues if issue.blocking)
        super().__init__(message or "scene asset preflight failed")


# AUD-29803: caps for data-URI inlining.  ``data_uri`` used to read any
# referenced file whole and base64 it into every rendered SVG, so a huge
# asset (or a non-image one masquerading as a scene asset) bloated each
# frame by ~4/3 of the file size.  Assets above the raw-byte cap or outside
# the ``image/*`` family are not inlined; callers already degrade ``None``
# to the same placeholder as a missing asset.
MAX_DATA_URI_ASSET_BYTES = 8 * 1024 * 1024
_DATA_URI_MIME_PREFIX = "image/"


class AssetResolver:
    """Resolve only assets inside the explicit render root."""

    def __init__(self, context: RenderContext) -> None:
        self.context = context
        self._root = Path(context.asset_root).resolve() if context.asset_root else None
        self._result = AssetPreflightResult(resolved={})

    def preflight(self, scene: TeachingSceneDocument) -> AssetPreflightResult:
        resolved: dict[str, Path] = {}
        issues: list[AssetIssue] = []
        for asset in scene.asset_refs:
            if not asset.path:
                issues.append(AssetIssue(asset.asset_id, asset.kind, "asset path is empty", self.context.strict_assets))
                continue
            if self._root is None:
                issues.append(AssetIssue(asset.asset_id, asset.kind, "asset_root is required for external assets", self.context.strict_assets))
                continue
            candidate = (self._root / asset.path).resolve()
            if candidate != self._root and self._root not in candidate.parents:
                issues.append(AssetIssue(asset.asset_id, asset.kind, f"asset path escapes asset_root: {asset.path}", True))
                continue
            if not candidate.is_file():
                issues.append(AssetIssue(asset.asset_id, asset.kind, f"missing {asset.kind} asset: {asset.path}", self.context.strict_assets))
                continue
            resolved[asset.asset_id] = candidate
        # Audio is scene-owned but still resolved from the same portable asset
        # table; missing audio must not disappear when video rendering works.
        known_ids = {asset.asset_id for asset in scene.asset_refs}
        for clip in audio_plan_for_scene(scene).clips:
            if clip.asset_ref not in known_ids:
                issues.append(AssetIssue(
                    clip.asset_ref, "audio", "audio plan references an unknown scene asset",
                    self.context.strict_assets,
                ))
        for element in scene.elements:
            for key in ("font_file", "font_path"):
                font_path = element.style.get(key) if isinstance(element.style, dict) else None
                if not font_path:
                    continue
                if self._root is None:
                    issues.append(AssetIssue(f"{element.element_id}:{key}", "font", "asset_root is required for an explicit font file", self.context.strict_assets))
                    continue
                candidate = (self._root / str(font_path)).resolve()
                if candidate != self._root and self._root not in candidate.parents:
                    issues.append(AssetIssue(f"{element.element_id}:{key}", "font", f"font path escapes asset_root: {font_path}", True))
                elif not candidate.is_file():
                    issues.append(AssetIssue(f"{element.element_id}:{key}", "font", f"missing font asset: {font_path}", self.context.strict_assets))
        self._result = AssetPreflightResult(resolved=resolved, issues=tuple(issues))
        if not self._result.is_valid:
            raise AssetPreflightError(self._result)
        return self._result

    def resolve(self, asset_id: str) -> Path | None:
        return self._result.resolved.get(asset_id)

    def data_uri(self, asset: SceneAssetRef) -> str | None:
        """Inline one asset as a data URI, bounded by the size/type caps.

        Returns ``None`` when the asset is unresolvable, unreadable, not an
        ``image/*`` type, or its raw size exceeds
        :data:`MAX_DATA_URI_ASSET_BYTES` (checked before the read and again
        on the actual bytes, so a file growing mid-render cannot smuggle a
        larger payload through).
        """
        path = self.resolve(asset.asset_id)
        if path is None:
            return None
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not mime.startswith(_DATA_URI_MIME_PREFIX):
            return None
        try:
            if path.stat().st_size > MAX_DATA_URI_ASSET_BYTES:
                return None
            raw = path.read_bytes()
        except OSError:
            return None
        if len(raw) > MAX_DATA_URI_ASSET_BYTES:
            return None
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def asset_by_id(self, scene: TeachingSceneDocument, asset_id: str) -> SceneAssetRef | None:
        return next((asset for asset in scene.asset_refs if asset.asset_id == asset_id), None)
