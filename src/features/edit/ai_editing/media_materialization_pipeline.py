"""生成候选的物化后 checksum/probe/QA/thumbnail/proxy 接缝（M11 B4）。

与既有 ``media_probe.py``（P8 导出质量探针）保持边界分离；本模块只处理生成素材。
"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Mapping, Protocol

from src.features.edit.ai_editing.generated_media_materializer import (
    GeneratedMediaMaterializer,
    MaterializeResult,
)
from src.features.edit.ai_editing.generation_models import GeneratedAsset
from src.features.edit.ai_editing.media_probe import FfprobeMediaProbe


@dataclass(frozen=True)
class GeneratedMediaProbe:
    sha256: str
    size_bytes: int
    mime_type: str
    duration_seconds: float | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    decoder: str = "python-metadata"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256, "size_bytes": self.size_bytes,
            "mime_type": self.mime_type, "duration_seconds": self.duration_seconds,
            "fps": self.fps, "width": self.width, "height": self.height,
            "decoder": self.decoder,
        }


class GeneratedProbeFn(Protocol):
    def __call__(self, path: str) -> Mapping[str, Any]: ...


class GeneratedMediaProbeService:
    """计算生成文件 checksum/MIME，并可接入受控 ffprobe 结果。"""

    def __init__(self, probe_fn: GeneratedProbeFn | None = None) -> None:
        self._probe_fn = probe_fn

    def probe(self, path: str) -> GeneratedMediaProbe:
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        extra = dict(self._probe_fn(path) if self._probe_fn else {})
        return GeneratedMediaProbe(
            sha256=digest.hexdigest(), size_bytes=size,
            mime_type=str(extra.get("mime_type") or mimetypes.guess_type(path)[0]
                         or "application/octet-stream"),
            duration_seconds=_as_float(extra.get("duration_seconds")),
            fps=_as_float(extra.get("fps")), width=_as_int(extra.get("width")),
            height=_as_int(extra.get("height")),
            decoder=str(extra.get("decoder", "python-metadata")),
        )


class FfprobeGeneratedMediaProbe:
    """Provider-neutral callable backed by the bounded ffprobe adapter."""

    def __init__(self, probe: FfprobeMediaProbe | None = None) -> None:
        self._probe = probe or FfprobeMediaProbe(include_output_hash=False)

    def __call__(self, path: str) -> Mapping[str, Any]:
        result = self._probe.probe(path)
        if not result.succeeded:
            raise RuntimeError(result.error_code or "generated_media_probe_failed")
        quality = result.quality_input
        metadata = result.metadata or {}
        return {
            "mime_type": _mime_type(path),
            "duration_seconds": quality.duration_seconds,
            "fps": quality.fps,
            "width": quality.resolution[0] if quality.resolution else None,
            "height": quality.resolution[1] if quality.resolution else None,
            "decoder": "ffprobe",
            **metadata,
        }


@dataclass(frozen=True)
class GeneratedMediaValidationPolicy:
    max_bytes: int = 512 * 1024 * 1024
    allowed_mime_prefixes: tuple[str, ...] = ("video/", "audio/", "image/")
    expected_duration_seconds: float | None = None
    duration_tolerance_seconds: float = 0.25

    def validate(self, probe: GeneratedMediaProbe) -> tuple[str, ...]:
        issues: list[str] = []
        if probe.size_bytes <= 0:
            issues.append("empty_media")
        if probe.size_bytes > self.max_bytes:
            issues.append("media_size_exceeds_limit")
        if self.allowed_mime_prefixes and not any(
            probe.mime_type.startswith(prefix) for prefix in self.allowed_mime_prefixes
        ):
            issues.append("mime_type_not_allowed")
        if (
            self.expected_duration_seconds is not None
            and probe.duration_seconds is not None
            and abs(probe.duration_seconds - self.expected_duration_seconds)
            > self.duration_tolerance_seconds
        ):
            issues.append("duration_out_of_tolerance")
        return tuple(issues)


@dataclass(frozen=True)
class MaterializedGeneratedMedia:
    asset_id: str
    path: str
    probe: GeneratedMediaProbe
    thumbnail_path: str = ""
    proxy_path: str = ""
    qa_issues: tuple[str, ...] = ()

    @property
    def importable(self) -> bool:
        return not self.qa_issues

    def to_dict(self) -> dict[str, Any]:
        # path 只用于当前进程，不进入工程/provenance JSON。
        return {
            "asset_id": self.asset_id, "probe": self.probe.to_dict(),
            "has_thumbnail": bool(self.thumbnail_path), "has_proxy": bool(self.proxy_path),
            "qa_issues": list(self.qa_issues), "importable": self.importable,
        }


GeneratedMediaBuilder = Callable[[str, str], str]


class GeneratedMediaMaterializationPipeline:
    """物化 → probe/QA → 可选 thumbnail/proxy 的纯服务层。"""

    def __init__(
        self, materializer: GeneratedMediaMaterializer, *,
        probe_service: GeneratedMediaProbeService | None = None,
        policy: GeneratedMediaValidationPolicy | None = None,
        thumbnail_builder: GeneratedMediaBuilder | None = None,
        proxy_builder: GeneratedMediaBuilder | None = None,
    ) -> None:
        self._materializer = materializer
        self._probe_service = probe_service or GeneratedMediaProbeService(
            FfprobeGeneratedMediaProbe(),
        )
        self._policy = policy or GeneratedMediaValidationPolicy()
        self._thumbnail_builder = thumbnail_builder
        self._proxy_builder = proxy_builder

    def materialize(
        self, asset: GeneratedAsset,
    ) -> tuple[MaterializedGeneratedMedia | None, MaterializeResult]:
        result = self._materializer.materialize(asset)
        if not result.succeeded or not result.path:
            return None, result
        try:
            probe = self._probe_service.probe(result.path)
        except Exception:  # noqa: BLE001 — corrupt/unavailable media is a QA block
            probe = GeneratedMediaProbe(
                sha256=result.sha256, size_bytes=result.size_bytes,
                mime_type=result.mime_type, decoder="unavailable",
            )
            return MaterializedGeneratedMedia(
                asset_id=asset.asset_id, path=result.path, probe=probe,
                qa_issues=("media_probe_failed",),
            ), result
        issues = self._policy.validate(probe)
        thumbnail = (
            self._thumbnail_builder(result.path, asset.asset_id)
            if not issues and self._thumbnail_builder else ""
        )
        proxy = (
            self._proxy_builder(result.path, asset.asset_id)
            if not issues and self._proxy_builder else ""
        )
        return MaterializedGeneratedMedia(
            asset_id=asset.asset_id, path=result.path, probe=probe,
            thumbnail_path=thumbnail, proxy_path=proxy, qa_issues=issues,
        ), result


def _as_float(value: Any) -> float | None:
    """Coerce an external probe value to ``float``, accepting rational strings.

    AUD-13908：内置后端把帧率报成 ffprobe/PyAV 的有理数形式（如
    ``"30000/1001"``、``"30/1"``），``float()`` 对它们抛 ValueError，导致
    ``GeneratedMediaProbe.fps`` 恒为 None。先按普通数值解析，失败后再按
    ``Fraction("num/den")`` 解析；仍失败（含 ``"30/0"`` 之类的除零）返回 None。
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return float(Fraction(str(value)))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _mime_type(path: str) -> str:
    import mimetypes
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


__all__ = [
    "GeneratedMediaProbe", "GeneratedMediaProbeService", "FfprobeGeneratedMediaProbe",
    "GeneratedMediaValidationPolicy", "MaterializedGeneratedMedia",
    "GeneratedMediaMaterializationPipeline",
]
