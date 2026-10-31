"""Adaptive transcript routing: Qwen3-ASR first, optional Demucs/WhisperX fallback."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import replace
from typing import Any

from src.features.edit.ai_editing.analysis_models import (
    AssetAnalysis,
    AssetFingerprint,
    FieldProvenance,
    analysis_to_segments,
)
from src.features.edit.ai_editing.editorial_models import SegmentKind, SemanticSegment

logger = logging.getLogger(__name__)


class AdaptiveTranscriptProvider:
    """Route one transcript request without coupling the timeline to a model.

    Modes:
      - ``qwen``: Qwen3-ASR only;
      - ``whisperx``: explicit WhisperX fallback;
      - ``demucs``: Qwen3-ASR after Demucs vocals separation;
      - ``auto``: Qwen first, then Demucs when quality is low, then WhisperX.
    """

    provider_id = "transcript_auto"
    supported_kinds = (SegmentKind.TRANSCRIPT,)

    def __init__(
        self,
        qwen_provider: Any,
        *,
        whisperx_provider: Any | None = None,
        demucs_preprocessor: Any | None = None,
        mode: str | None = None,
        quality_threshold: float = 0.65,
    ) -> None:
        self._qwen = qwen_provider
        self._whisperx = whisperx_provider
        self._demucs = demucs_preprocessor
        self.mode = (mode or os.environ.get("TOPOS_TRANSCRIPT_MODE", "auto")).lower()
        self.quality_threshold = max(0.0, min(1.0, float(quality_threshold)))

    def validate_options(self, options: dict[str, Any] | None = None) -> str | None:
        """Validate an explicit request before starting a worker thread."""
        request_options = dict(options or {})
        if str(request_options.get("background_music", "keep")) == "remove":
            if self._demucs is None:
                return "已选择去除背景音乐，但未配置 Demucs v4 人声分离器"
            if not self._is_available(self._demucs):
                reason = getattr(self._demucs, "unavailable_reason", None)
                detail = reason() if callable(reason) else "Demucs v4 模型包不可用"
                return f"已选择去除背景音乐，但无法启动人声分离：{detail}"
        validator = getattr(self._qwen, "validate_options", None)
        if callable(validator):
            return validator(request_options)
        return None

    def analyze(
        self,
        asset_id: str,
        fingerprint: AssetFingerprint,
        *,
        fps: str,
        progress: Any | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        mode = self.mode
        request_options = dict(options or {})
        force_demucs = str(request_options.get("background_music", "keep")) == "remove"
        # AUD-14910："可降级，必标注"。用户显式要求去除背景音乐但分离运行失败时，
        # 选择软降级（保住 transcript 结果）而非与"分离器不可用"路径一致的硬失败，
        # 但降级必须写入 provenance.extra，否则用户会误以为成品已去伴奏。
        demucs_degradation: str | None = None
        if mode == "whisperx":
            return self._run_whisperx(asset_id, fingerprint, fps, progress)
        if mode == "demucs" or force_demucs:
            if self._demucs is not None and self._is_available(self._demucs):
                try:
                    demucs_result = self._run_demucs(
                        asset_id, fingerprint, fps, progress, request_options,
                    )
                    return self._annotate(
                        demucs_result,
                        "qwen3_asr_demucs_forced" if force_demucs else "qwen3_asr_demucs_v4",
                    )
                except Exception as exc:
                    if mode == "demucs":
                        raise
                    logger.warning("Forced Demucs preprocessing unavailable: %s", exc)
                    _emit_progress(progress, None, f"人声分离不可用，继续使用原声：{exc}")
                    demucs_degradation = f"{type(exc).__name__}: {exc}"
            elif mode == "demucs":
                raise RuntimeError("已选择人声分离，但 Demucs 模型不可用")
            elif force_demucs:
                reason = getattr(self._demucs, "unavailable_reason", None)
                detail = reason() if callable(reason) else "Demucs v4 模型包不可用"
                raise RuntimeError(f"已选择去除背景音乐，但无法启动人声分离：{detail}")
            else:
                _emit_progress(progress, None, "未安装 Demucs，继续使用原声识别")
        elif force_demucs:
            raise RuntimeError("已选择去除背景音乐，但未配置 Demucs v4 人声分离器")

        qwen_result: tuple[AssetAnalysis, tuple[SemanticSegment, ...]] | None = None
        qwen_error: Exception | None = None
        try:
            qwen_result = self._qwen.analyze(
                asset_id, fingerprint, fps=fps, progress=progress, options=request_options,
            )
            if self._quality(qwen_result[0]) >= self.quality_threshold:
                return self._annotate(
                    qwen_result, "qwen3_asr_original", demucs_degradation=demucs_degradation,
                )
        except Exception as exc:
            qwen_error = exc
            logger.warning("Qwen3-ASR route failed; trying fallbacks: %s", exc)
            _emit_progress(progress, None, f"Qwen3-ASR 失败，正在尝试备用方案：{exc}")

        # Demucs is attempted only after the original result is weak/failed and
        # the optional backend is available.  It never runs for every asset.
        best_result = qwen_result
        best_selection = "qwen3_asr_original_low_confidence"
        if self._demucs is not None and self._is_available(self._demucs):
            try:
                demucs_result = self._run_demucs(
                    asset_id, fingerprint, fps, progress, request_options,
                )
                if best_result is None or self._quality(demucs_result[0]) > self._quality(best_result[0]):
                    best_result = demucs_result
                    best_selection = "qwen3_asr_demucs_v4"
                    # 分离最终成功，之前请求失败的降级痕迹不再适用。
                    demucs_degradation = None
                if self._quality(demucs_result[0]) >= self.quality_threshold:
                    return self._annotate(demucs_result, "qwen3_asr_demucs_v4")
            except Exception as exc:
                logger.warning("Demucs fallback unavailable: %s", exc)
                _emit_progress(progress, None, f"人声分离备用方案失败：{exc}")

        if self._whisperx is not None and self._is_available(self._whisperx):
            try:
                whisper_result = self._run_whisperx(asset_id, fingerprint, fps, progress)
                if whisper_result[0].transcript or qwen_result is None:
                    return self._annotate(
                        whisper_result, "whisperx_fallback",
                        demucs_degradation=demucs_degradation,
                    )
            except Exception as exc:
                logger.warning("WhisperX fallback unavailable: %s", exc)
                _emit_progress(progress, None, f"WhisperX 备用方案失败：{exc}")

        if best_result is not None:
            return self._annotate(
                best_result, best_selection, demucs_degradation=demucs_degradation,
            )
        if qwen_error is not None:
            raise qwen_error
        raise RuntimeError("没有可用的转录 provider")

    def _run_demucs(
        self, asset_id: str, fingerprint: AssetFingerprint, fps: str,
        progress: Any, options: dict[str, Any] | None = None,
    ) -> Any:
        return self._qwen.analyze_with_preprocessor(
            asset_id,
            fingerprint,
            fps=fps,
            preprocessor=self._demucs,
            progress=progress,
            options=options,
        )

    def _run_whisperx(self, asset_id: str, fingerprint: AssetFingerprint, fps: str, progress: Any) -> Any:
        if self._whisperx is None:
            raise RuntimeError("WhisperX provider 未配置")
        return self._whisperx.analyze(
            asset_id, fingerprint, fps=fps, progress=progress
        )

    @staticmethod
    def _is_available(backend: Any) -> bool:
        checker = getattr(backend, "is_available", None)
        return bool(checker()) if callable(checker) else True

    @staticmethod
    def _quality(analysis: AssetAnalysis) -> float:
        transcript = tuple(analysis.transcript or ())
        if not transcript:
            return 0.0
        confidence = sum(max(0.0, min(1.0, float(s.confidence))) for s in transcript)
        score = confidence / len(transcript)
        if not any(s.words for s in transcript):
            score -= 0.05
        return max(0.0, min(1.0, score))

    @staticmethod
    def _annotate(
        result: tuple[AssetAnalysis, tuple[SemanticSegment, ...]],
        selection: str,
        *,
        demucs_degradation: str | None = None,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        analysis, _segments = result
        provenance = analysis.provenance
        extra = dict(provenance.extra or {})
        extra["router_selection"] = selection
        if demucs_degradation:
            # AUD-14910：请求过伴奏去除但被跳过时，持久化的 analysis 必须留有痕迹。
            extra["demucs_requested_but_failed"] = demucs_degradation
        updated_provenance = replace(provenance, extra=extra)
        updated = replace(
            analysis,
            provenance=updated_provenance,
            field_provenance=FieldProvenance.single("transcript", updated_provenance),
        )
        return updated, tuple(analysis_to_segments(updated))


class ProfileAwareTranscriptProvider:
    """Select local Qwen or a configured cloud ASR profile per request.

    ``ProfileRegistry`` keeps one active profile per capability.  If no ASR
    profile is active, the local adaptive chain remains the safe default.
    Remote profiles are instantiated lazily so opening Preferences never makes
    a network call or loads a cloud adapter unnecessarily.
    """

    # Keep the historical id so existing clip actions and persisted recipes
    # transparently gain profile routing.
    provider_id = "transcript_auto"
    supported_kinds = (SegmentKind.TRANSCRIPT,)

    def __init__(
        self,
        local_provider: Any,
        registry: Any,
        credential_store: Any,
        *,
        asset_path_provider: Any | None = None,
    ) -> None:
        self._local = local_provider
        self._registry = registry
        self._credential_store = credential_store
        self._asset_path_provider = asset_path_provider
        self._remote_by_profile: dict[str, Any] = {}
        # AUD-14912：缓存字典加锁——并发 analyze 时两个线程可能同时看到
        # miss 并各自实例化一个 RemoteTranscriptProvider（重复构造/重复握手），
        # 且 dict 并发写本身有竞态。双检锁：命中路径无锁开销，miss 才串行化。
        self._remote_lock = threading.Lock()

    @property
    def active_profile(self) -> Any | None:
        getter = getattr(self._registry, "active_profile_for_capability", None)
        if callable(getter):
            profile = getter("asr")
            if profile is not None:
                return profile
        for profile in getattr(self._registry, "list", lambda: [])():
            capabilities = set(getattr(profile, "capabilities", ()) or ())
            if "asr" in capabilities or getattr(profile, "provider_kind", "") == "local_qwen_asr":
                return profile
        return None

    def _remote(self, profile: Any) -> Any:
        # AUD-14912：双检锁——先无锁快照命中（常见路径），miss 才进锁内二次
        # 确认后构造，保证每个 profile 恰好实例化一次。
        provider = self._remote_by_profile.get(profile.profile_id)
        if provider is not None:
            return provider
        with self._remote_lock:
            provider = self._remote_by_profile.get(profile.profile_id)
            if provider is None:
                from src.features.edit.ai_editing.remote_transcript_provider import (
                    RemoteTranscriptProvider,
                )
                provider = RemoteTranscriptProvider(
                    profile,
                    self._credential_store,
                    asset_path_provider=self._asset_path_provider,
                )
                self._remote_by_profile[profile.profile_id] = provider
            return provider

    def _use_remote(self, profile: Any | None) -> bool:
        if profile is None:
            return False
        source = str(getattr(profile, "source_kind", "local"))
        kind = str(getattr(profile, "provider_kind", ""))
        return source not in {"local", "ollama"} and kind != "local_qwen_asr"

    def validate_options(self, options: dict[str, Any] | None = None) -> str | None:
        profile = self.active_profile
        if self._use_remote(profile):
            return self._remote(profile).validate_options(options)
        validator = getattr(self._local, "validate_options", None)
        return validator(options) if callable(validator) else None

    def analyze(
        self,
        asset_id: str,
        fingerprint: AssetFingerprint,
        *,
        fps: str,
        progress: Any | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        profile = self.active_profile
        if self._use_remote(profile):
            return self._remote(profile).analyze(
                asset_id, fingerprint, fps=fps, progress=progress, options=options,
            )
        return self._local.analyze(
            asset_id, fingerprint, fps=fps, progress=progress, options=options,
        )


def _emit_progress(progress: Any | None, fraction: float | None, message: str) -> None:
    """转发路由阶段状态；状态栏回调异常不能中断备用链。"""
    if progress is None:
        return
    try:
        progress(fraction, message)
    except Exception:  # noqa: BLE001
        logger.debug("Transcript router progress callback failed", exc_info=True)


__all__ = ["AdaptiveTranscriptProvider", "ProfileAwareTranscriptProvider"]
