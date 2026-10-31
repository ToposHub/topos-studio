"""Production adapter for shot-level visual semantic analysis.

The contracts in :mod:`visual_semantics` deliberately contain no model loading
or media-path access.  This module is the composition boundary that turns a
user-approved asset into evidence only:

* it samples the manifest's head/middle/tail frames, never a whole timeline;
* it accepts only explicitly non-demo inference backends;
* captions/tags stay untrusted evidence and embeddings stay in rebuildable
  cache, outside project payloads;
* it implements ``AnalysisProvider`` so scheduler, cancellation and search use
  the same path as OCR/transcript analysis.

There are intentionally no deterministic fallbacks here.  A missing verified
model/backend is an actionable analysis failure, never fake visual evidence.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
)
from src.features.edit.ai_editing.model_pack_manager import ModelPackNotInstalledError
from src.features.edit.ai_editing.editorial_models import FrameRange, SegmentKind, SemanticSegment
from src.features.edit.ai_editing.keyframe_sampler import (
    FrameSamplerProtocol,
    ShotSpan,
    build_keyframe_manifest,
    derive_shot_spans,
)
from src.features.edit.ai_editing.visual_semantics import (
    VISUAL_EMBEDDING,
    VisualCacheStore,
    VisualEvidence,
    VisualKeyframe,
    VisualObservation,
    aggregate_shot_observations,
    keyframes_from_manifest,
)

logger = logging.getLogger(__name__)


class VisualSemanticUnavailableError(RuntimeError):
    """No verified, non-demo visual backend is available for this request."""


class _NoDecodeSampler:
    """Identity-only 关键帧构建用的不解码 sampler（``sample`` 恒返回 None）。

    AUD-15001：``keyframes_from_manifest`` 会为每个 entry 调
    ``sampler.sample(frame)`` 附着整帧 ndarray。用它构建 identity-only
    关键帧（image=None），解码推迟到推理循环内逐帧进行。
    """

    def sample(self, frame_index: int) -> Any:
        return None


class VideoAssetFrameSampler:
    """Small lifecycle wrapper around the existing VFR-safe video sampler.

    It is created by the app composition root after resolving an already-known
    media asset.  The analysis core never receives a media path or gains
    arbitrary file-system access.
    """

    def __init__(self, source_path: str) -> None:
        if not source_path:
            raise ValueError("source_path is required for visual analysis")
        from src.features.edit.core.scene_cut.frame_sampler import VideoFrameSampler

        self._sampler = VideoFrameSampler(source_path)
        self._sampler.open()

    def sample(self, frame_index: int) -> Any:
        sampled = self._sampler.sample_single_frame(int(frame_index))
        return getattr(sampled, "frame_data", None) if sampled is not None else None

    def close(self) -> None:
        self._sampler.close()


class VaultCaptionBackend:
    """Adapt an installed Vault captioner to the visual keyframe contract."""

    is_demo = False

    def __init__(self, captioner: Any) -> None:
        self._captioner = captioner
        self.provider_id = str(getattr(captioner, "provider_id", "visual-caption"))
        self.model_version = self.provider_id

    def infer(self, keyframe: VisualKeyframe) -> tuple[VisualObservation, ...]:
        with _keyframe_image_file(keyframe) as image_path:
            result = self._captioner.caption_image(image_path)
        model = str(getattr(result, "model", "") or self.model_version)
        provenance = {"provider_id": self.provider_id, "model_version": model}
        output: list[VisualObservation] = []
        caption = str(getattr(result, "caption", "") or "").strip()
        if caption:
            output.append(VisualObservation(
                asset_id=keyframe.asset_id, shot_index=keyframe.shot_index,
                frame=keyframe.frame, kind="caption", value=caption,
                confidence=0.70, field="caption", provenance=provenance,
            ))
        for label in getattr(result, "tags", ()) or ():
            text = str(label).strip()
            if text:
                output.append(VisualObservation(
                    asset_id=keyframe.asset_id, shot_index=keyframe.shot_index,
                    frame=keyframe.frame, kind="tag", value=text,
                    confidence=0.65, field="tag", provenance=provenance,
                ))
        return tuple(output)


class VaultEmbeddingBackend:
    """Adapt an installed Vault image embedding model to keyframe evidence."""

    is_demo = False

    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder
        self.provider_id = str(getattr(embedder, "provider_id", "visual-embedding"))
        self.model_version = self.provider_id

    def infer(self, keyframe: VisualKeyframe) -> tuple[VisualObservation, ...]:
        with _keyframe_image_file(keyframe) as image_path:
            result = self._embedder.embed_image(image_path)
        values = tuple(float(value) for value in (getattr(result, "vector", ()) or ()))
        if not values:
            return ()
        model = str(getattr(result, "model", "") or self.model_version)
        return (VisualObservation(
            asset_id=keyframe.asset_id, shot_index=keyframe.shot_index,
            frame=keyframe.frame, kind=VISUAL_EMBEDDING,
            confidence=0.70, embedding=values,
            provenance={"provider_id": self.provider_id, "model_version": model},
        ),)


@dataclass(frozen=True)
class _EmbeddingAnchor:
    segment: SemanticSegment
    vector: tuple[float, ...]


class VisualSemanticAnalysisProvider:
    """Scheduler-compatible visual tag/caption/embedding analysis provider."""

    provider_id = "visual_semantic_local"
    supported_kinds = (SegmentKind.TAG, SegmentKind.VISUAL)

    def __init__(
        self,
        *,
        sampler_provider: Callable[[str], FrameSamplerProtocol],
        total_frames_provider: Callable[[str], int],
        scene_cuts_provider: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
        visual_providers: Iterable[Any] = (),
        cache_store: VisualCacheStore | None = None,
        unavailable_reason: str = "",
    ) -> None:
        self._sampler_provider = sampler_provider
        self._total_frames_provider = total_frames_provider
        self._scene_cuts_provider = scene_cuts_provider or (lambda _asset_id: ())
        self._visual_providers = tuple(visual_providers)
        self._cache_store = cache_store
        self._unavailable_reason = unavailable_reason
        self._pending_embeddings: dict[str, dict[str, tuple[float, ...]]] = {}

    def is_available(self) -> bool:
        return bool(self._eligible_providers())

    def analyze(
        self,
        asset_id: str,
        fingerprint: AssetFingerprint,
        *,
        fps: str,
        progress: Any | None = None,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        providers = self._eligible_providers()
        if not providers:
            reason = self._unavailable_reason or (
                "未安装经验证的视觉 caption/tag/embedding 模型；"
                "演示 provider 不可用于产品分析"
            )
            raise VisualSemanticUnavailableError(reason)

        total_frames = max(0, int(self._total_frames_provider(asset_id) or 0))
        if total_frames <= 0:
            raise ValueError(f"cannot analyze {asset_id!r}: total frame count is unavailable")
        scene_cuts = tuple(dict(item) for item in self._scene_cuts_provider(asset_id) or ())
        shot_spans = derive_shot_spans(scene_cuts, total_frames=total_frames)
        if not shot_spans:
            raise ValueError(f"cannot derive shot spans for {asset_id!r}")
        manifest = build_keyframe_manifest(shot_spans, asset_id=asset_id)
        sampler = self._sampler_provider(asset_id)
        try:
            # AUD-15001：不在推理前一次性物化全部关键帧解码图像（4K RGB 单帧约
            # 24MB，500 镜头的长片 ≈ 1500 帧 ≈ 36GB 常驻内存，直接 OOM 风险）。
            # 先用不解码的 sampler 构建 identity-only 关键帧（复用 manifest 的
            # shot/range 校验），推理循环内逐帧解码、单帧所有 backend 完成后
            # 立即释放引用（aggregation 只用 identity，不读 image）。
            identities = keyframes_from_manifest(manifest, shot_spans, _NoDecodeSampler())
            if not identities:
                raise ValueError(f"no keyframes available for {asset_id!r}")

            observed: list[tuple[VisualKeyframe, tuple[VisualObservation, ...]]] = []
            embedding_observations: list[VisualObservation] = []
            total_steps = max(1, len(identities) * len(providers))
            step = 0
            for identity in identities:
                keyframe: VisualKeyframe | None = None
                keyframe_observations: list[VisualObservation] = []
                try:
                    keyframe = replace(identity, image=sampler.sample(identity.frame))
                    for backend in providers:
                        result = tuple(backend.infer(keyframe) or ())
                        step += 1
                        if progress is not None:
                            progress(step / total_steps, f"{getattr(backend, 'provider_id', 'visual')} keyframe")
                        for observation in result:
                            normalized = self._normalize_observation(observation, keyframe, backend)
                            keyframe_observations.append(normalized)
                            if normalized.kind == VISUAL_EMBEDDING:
                                embedding_observations.append(normalized)
                finally:
                    # 释放本帧解码图像（identity 不携带 image，可长期持有）。
                    keyframe = None
                observed.append((identity, tuple(keyframe_observations)))
        finally:
            closer = getattr(sampler, "close", None)
            if callable(closer):
                closer()

        aggregation = aggregate_shot_observations(observed)
        segments = [self._segment_with_conflicts(item, aggregation.conflicts)
                    for item in aggregation.evidence]
        anchors = self._build_embedding_anchors(asset_id, shot_spans, embedding_observations)
        segments.extend(anchor.segment for anchor in anchors)
        embeddings = {anchor.segment.segment_id: anchor.vector for anchor in anchors}
        self._pending_embeddings[asset_id] = embeddings

        if self._cache_store is not None:
            self._cache_store.put(
                asset_id=asset_id,
                keyframe_manifest=manifest,
                evidence=aggregation.evidence,
                embeddings=embeddings,
                fingerprint=fingerprint.to_dict(),
            )
        provenance = AnalysisProvenance(
            provider=self.provider_id,
            model=", ".join(sorted({str(getattr(p, "model_version", "")) for p in providers})),
            extra={
                "keyframe_manifest": manifest.to_dict(),
                "providers": [str(getattr(p, "provider_id", "")) for p in providers],
                "conflicts": [item.to_dict() for item in aggregation.conflicts],
                "cache_rebuildable": self._cache_store is not None,
            },
        )
        analysis = AssetAnalysis(
            asset_id=asset_id, fingerprint=fingerprint, fps=fps, provenance=provenance,
        )
        return analysis, tuple(segments)

    def drain_embeddings(self, asset_id: str) -> Mapping[str, Sequence[float]]:
        """Return only cache-rebuildable vectors for the just-finished asset."""
        return self._pending_embeddings.pop(asset_id, {})

    def invalidate_asset(self, asset_id: str) -> None:
        """Delete all rebuildable visual state for one asset."""
        self._pending_embeddings.pop(asset_id, None)
        if self._cache_store is not None:
            self._cache_store.delete_asset(asset_id)

    def _eligible_providers(self) -> tuple[Any, ...]:
        return tuple(
            provider for provider in self._visual_providers
            if not bool(getattr(provider, "is_demo", False))
        )

    @staticmethod
    def _normalize_observation(
        observation: VisualObservation,
        keyframe: VisualKeyframe,
        backend: Any,
    ) -> VisualObservation:
        if observation.asset_id != keyframe.asset_id or observation.shot_index != keyframe.shot_index:
            raise ValueError("visual backend observation does not match requested keyframe")
        if observation.frame != keyframe.frame:
            raise ValueError("visual backend observation frame does not match requested keyframe")
        provenance = dict(observation.provenance)
        provenance.setdefault("provider_id", str(getattr(backend, "provider_id", "visual")))
        provenance.setdefault("model_version", str(getattr(backend, "model_version", "")))
        provenance["trust"] = "untrusted"
        return replace(observation, provenance=provenance)

    @staticmethod
    def _segment_with_conflicts(
        evidence: VisualEvidence,
        conflicts: Sequence[Any],
    ) -> SemanticSegment:
        segment = evidence.to_segment()
        matching = [conflict.to_dict() for conflict in conflicts
                    if conflict.asset_id == evidence.asset_id and conflict.shot_index == evidence.shot_index]
        if not matching:
            return segment
        provenance = dict(segment.provenance)
        provenance["conflicts"] = matching
        return replace(segment, provenance=provenance)

    @staticmethod
    def _build_embedding_anchors(
        asset_id: str,
        spans: Sequence[ShotSpan],
        observations: Sequence[VisualObservation],
    ) -> tuple[_EmbeddingAnchor, ...]:
        by_key: dict[tuple[int, str, int], list[VisualObservation]] = {}
        for item in observations:
            provider_id = str(item.provenance.get("provider_id", "visual-embedding"))
            by_key.setdefault((item.shot_index, provider_id, len(item.embedding)), []).append(item)
        span_by_index = {span.shot_index: span for span in spans}
        anchors: list[_EmbeddingAnchor] = []
        for (shot_index, provider_id, dimension), items in sorted(by_key.items()):
            span = span_by_index.get(shot_index)
            if span is None or dimension <= 0:
                continue
            values = tuple(sum(item.embedding[index] for item in items) / len(items)
                           for index in range(dimension))
            norm = math.sqrt(sum(value * value for value in values))
            if not norm or not all(math.isfinite(value) for value in values):
                continue
            vector = tuple(value / norm for value in values)
            digest = hashlib.sha256(
                f"{asset_id}|{shot_index}|{provider_id}|{dimension}".encode("utf-8")
            ).hexdigest()[:20]
            segment_id = f"visual-embedding-{digest}"
            segment = SemanticSegment(
                segment_id=segment_id,
                asset_id=asset_id,
                source_range=FrameRange(span.start_frame, span.duration),
                kind=SegmentKind.VISUAL,
                payload={
                    "visual_embedding": True,
                    "source_frames": sorted({item.frame for item in items}),
                    "shot_index": shot_index,
                    "trust": "untrusted",
                },
                confidence=sum(item.confidence for item in items) / len(items),
                evidence_ref=segment_id,
                provenance={
                    "provider_id": provider_id,
                    "model_version": str(items[0].provenance.get("model_version", "")),
                    "aggregation": "shot_mean_normalized",
                    "trust": "untrusted",
                },
            )
            anchors.append(_EmbeddingAnchor(segment=segment, vector=vector))
        return tuple(anchors)


def build_vault_visual_provider(
    *,
    model_pack_manager: Any,
    sampler_provider: Callable[[str], FrameSamplerProtocol],
    total_frames_provider: Callable[[str], int],
    scene_cuts_provider: Callable[[str], Sequence[Mapping[str, Any]]],
    cache_root: str | Path,
) -> VisualSemanticAnalysisProvider:
    """Build the production adapter from verified optional Vault model packs.

    Both adapters are opt-in and require the main application's strict model
    manager.  A HuggingFace cache or a directory alone can therefore never
    bypass release SHA verification.
    """
    backends: list[Any] = []
    missing: list[str] = []
    try:
        model_pack_manager.require_usable("qwen3_vl_8b_awq")
        from apps.topos_vault.providers.real_visual_caption import Qwen3VLCaptioner
        captioner = Qwen3VLCaptioner()
        if captioner.is_available():
            backends.append(VaultCaptionBackend(captioner))
        else:
            missing.append("qwen3_vl_8b_awq runtime dependency")
    except (ImportError, ModelPackNotInstalledError) as exc:
        # 模型包未安装/未通过 SHA 校验，或 provider 模块缺失——是可行动的
        # "缺模型包"提示（AUD-15000：仅此类异常归入该类别）。
        logger.warning("qwen3_vl_8b_awq unavailable (%s); treated as missing model pack", exc)
        missing.append("qwen3_vl_8b_awq verified model pack")
    except Exception as exc:
        # AUD-15000：构造期真实错误（依赖冲突、显存不足、torch 版本不匹配、
        # 代码 bug）不得伪装成"缺模型包"——保留 traceback，并把异常摘要拼进
        # unavailable_reason 供用户/排查定位。
        logger.exception("Qwen3VLCaptioner assembly failed unexpectedly")
        missing.append(f"qwen3_vl_8b_awq init failed: {type(exc).__name__}: {exc}")
    try:
        model_pack_manager.require_usable("siglip2_base")
        from apps.topos_vault.providers.real_visual_embed import SigLIP2EmbeddingProvider
        embedder = SigLIP2EmbeddingProvider()
        if embedder.is_available():
            backends.append(VaultEmbeddingBackend(embedder))
        else:
            missing.append("siglip2_base runtime dependency")
    except (ImportError, ModelPackNotInstalledError) as exc:
        logger.warning("siglip2_base unavailable (%s); treated as missing model pack", exc)
        missing.append("siglip2_base verified model pack")
    except Exception as exc:
        logger.exception("SigLIP2EmbeddingProvider assembly failed unexpectedly")
        missing.append(f"siglip2_base init failed: {type(exc).__name__}: {exc}")
    return VisualSemanticAnalysisProvider(
        sampler_provider=sampler_provider,
        total_frames_provider=total_frames_provider,
        scene_cuts_provider=scene_cuts_provider,
        visual_providers=backends,
        cache_store=VisualCacheStore(cache_root),
        unavailable_reason="缺少 " + "、".join(missing) if not backends else "",
    )


class _keyframe_image_file:
    """Materialize one decoded frame for a path-only third-party backend.

    The temporary PNG exists only for the single inference call and is removed
    even on provider errors.  It is not part of the project or visual cache.
    """

    def __init__(self, keyframe: VisualKeyframe) -> None:
        self._image = keyframe.image
        self._path = ""

    def __enter__(self) -> str:
        if self._image is None:
            raise ValueError("visual backend received an empty decoded keyframe")
        from PIL import Image

        image = self._image
        if hasattr(image, "shape") and len(getattr(image, "shape", ())) >= 3:
            # Existing video samplers yield BGR ndarray; Pillow expects RGB.
            image = image[:, :, :3][:, :, ::-1]
        if not isinstance(image, Image.Image):
            image = Image.fromarray(image)
        fd, path = tempfile.mkstemp(prefix="topos-visual-", suffix=".png")
        os.close(fd)
        self._path = path
        image.convert("RGB").save(path, format="PNG")
        return path

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._path:
            try:
                os.remove(self._path)
            except FileNotFoundError:
                pass


__all__ = [
    "VaultCaptionBackend", "VaultEmbeddingBackend", "VideoAssetFrameSampler",
    "VisualSemanticAnalysisProvider", "VisualSemanticUnavailableError",
    "build_vault_visual_provider",
]
