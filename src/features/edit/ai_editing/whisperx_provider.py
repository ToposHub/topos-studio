"""Optional WhisperX transcript provider.

WhisperX is deliberately not part of the default installation.  It is a
fallback for languages or clips where Qwen3-ASR needs a different alignment or
diarization path.  The provider keeps the same ``AssetAnalysis`` contract as
Qwen, so routing does not affect subtitle placement.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import os
import threading
from typing import Any, Callable

from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
    FieldProvenance,
    TranscriptSegment,
    WordSegment,
    analysis_to_segments,
)
from src.features.edit.ai_editing.audio_extractor import AudioExtractor
from src.features.edit.ai_editing.editorial_models import SegmentKind, SemanticSegment
from src.features.edit.ai_editing.timebase import seconds_to_frame_range
from src.features.edit.ai_editing.transcript_provider import _real_inference_enabled
from src.shared.media.metadata import Rational

logger = logging.getLogger(__name__)


class WhisperXProvider:
    """Lazy, optional WhisperX implementation of ``AnalysisProvider``."""

    provider_id = "transcript_whisperx"
    supported_kinds = (SegmentKind.TRANSCRIPT,)

    def __init__(
        self,
        *,
        asset_path_provider: Callable[[str], str] | None = None,
        audio_extractor: AudioExtractor | None = None,
        model_name: str = "large-v3",
        device: str | None = None,
        allow_stub: bool = False,
        model_pack_manager: Any | None = None,
    ) -> None:
        self._asset_path_provider = asset_path_provider
        self._audio_extractor = audio_extractor or AudioExtractor()
        self._model_name = model_name
        self._device = device or os.environ.get("TOPOS_WHISPERX_DEVICE", "cpu")
        self._allow_stub = allow_stub
        self._model_pack_manager = model_pack_manager
        # AUD-15008：本 provider 以单实例注册进共享 AnalysisService，调度器
        # ThreadPoolExecutor（本地并发=2）可能并发调用 analyze。RLock 同时
        # 保护懒加载（双检锁，防止 large-v3 数 GB 模型被加载两次）和
        # transcribe（ctranslate2 模型对象不保证线程安全）。
        self._engine_lock = threading.RLock()
        self._engine: Any = None

    def is_available(self) -> bool:
        if importlib.util.find_spec("whisperx") is None:
            return False
        manager = self._model_pack_manager
        if manager is not None and getattr(manager, "get_spec", None):
            spec = manager.get_spec("whisperx_base")
            if spec is not None:
                status = manager.get_status("whisperx_base")
                return bool(getattr(status, "usable", False))
        return True

    def analyze(
        self,
        asset_id: str,
        fingerprint: AssetFingerprint,
        *,
        fps: str,
        progress: Any | None = None,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        real_enabled = _real_inference_enabled("TOPOS_WHISPERX_REAL")
        if not real_enabled:
            if not self._allow_stub:
                raise RuntimeError("WhisperX 备用 provider 未启用真实推理")
            return self._stub_result(asset_id, fingerprint, fps)
        if not self.is_available():
            raise RuntimeError("WhisperX 未安装或模型包未校验，无法启用备用 provider")
        path = self._asset_path_provider(asset_id) if self._asset_path_provider else ""
        wav_path = self._audio_extractor.extract_wav(path)
        try:
            import whisperx  # type: ignore[import-not-found]

            audio = whisperx.load_audio(wav_path)
            # AUD-15008：引擎懒加载与 transcribe 串行化（同一 RLock；重入安全）。
            with self._engine_lock:
                model = self._get_engine(whisperx)
                result = model.transcribe(audio, batch_size=4, language=None)
            segments = self._convert_segments(result, fps)
        finally:
            self._cleanup_temp_audio(wav_path)

        provenance = AnalysisProvenance(
            provider=self.provider_id,
            model=f"whisperx-{self._model_name}",
            extra={"alignment": "segment_or_word", "device": self._device},
        )
        analysis = AssetAnalysis(
            asset_id=asset_id,
            fingerprint=fingerprint,
            fps=fps,
            transcript=tuple(segments),
            provenance=provenance,
            field_provenance=FieldProvenance.single("transcript", provenance),
        )
        return analysis, tuple(analysis_to_segments(analysis))

    def _get_engine(self, whisperx: Any) -> Any:
        """惰性创建并缓存 WhisperX 引擎（AUD-15008：双检锁）。"""
        if self._engine is not None:
            return self._engine
        with self._engine_lock:
            if self._engine is not None:
                return self._engine
            compute_type = "float32" if self._device == "cpu" else "float16"
            self._engine = whisperx.load_model(
                self._model_name,
                self._device,
                compute_type=compute_type,
                language=None,
            )
            return self._engine

    @staticmethod
    def _convert_segments(result: dict[str, Any], fps: str) -> list[TranscriptSegment]:
        timeline_fps = Rational.from_string(fps)
        converted: list[TranscriptSegment] = []
        for item in result.get("segments", ()) or ():
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            start = float(item.get("start", 0.0) or 0.0)
            end = float(item.get("end", start) or start)
            frame_range = seconds_to_frame_range(start, max(start, end), timeline_fps)
            words: list[WordSegment] = []
            for word in item.get("words", ()) or ():
                word_text = str(word.get("word", word.get("text", ""))).strip()
                if not word_text or word.get("start") is None or word.get("end") is None:
                    continue
                # AUD-15007：word 级时间戳属外部数据（WhisperX 对齐器输出），
                # 须与段级同款防护——非有限值跳过，负值/倒置钳制，
                # 避免一条畸形 word 让整次已完成转录失败。
                w_start = float(word["start"])
                w_end = float(word["end"])
                if not (math.isfinite(w_start) and math.isfinite(w_end)):
                    continue
                w_start = max(0.0, w_start)
                w_end = max(w_start, w_end)
                word_range = seconds_to_frame_range(w_start, w_end, timeline_fps)
                words.append(
                    WordSegment(
                        start_frame=word_range.start_frame,
                        end_frame=word_range.end_frame,
                        text=word_text,
                        confidence=float(word.get("score", word.get("confidence", 1.0))),
                    )
                )
            if "confidence" in item:
                confidence = float(item.get("confidence", 0.8) or 0.8)
            elif "avg_logprob" in item:
                confidence = math.exp(float(item.get("avg_logprob", -0.2) or -0.2))
            else:
                confidence = 0.8
            confidence = max(0.0, min(1.0, confidence))
            converted.append(
                TranscriptSegment(
                    start_frame=frame_range.start_frame,
                    end_frame=frame_range.end_frame,
                    text=text,
                    confidence=confidence,
                    language=result.get("language"),
                    words=tuple(words),
                )
            )
        return converted

    def _stub_result(
        self, asset_id: str, fingerprint: AssetFingerprint, fps: str
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        provenance = AnalysisProvenance(
            provider=self.provider_id,
            model="whisperx-stub-v1",
            extra={"stub": True},
        )
        analysis = AssetAnalysis(
            asset_id=asset_id,
            fingerprint=fingerprint,
            fps=fps,
            transcript=(),
            provenance=provenance,
            field_provenance=FieldProvenance.single("transcript", provenance),
        )
        return analysis, ()

    @staticmethod
    def _cleanup_temp_audio(wav_path: str) -> None:
        try:
            if wav_path and os.path.isfile(wav_path):
                os.remove(wav_path)
        except OSError:
            logger.debug("WhisperX temp audio cleanup failed", exc_info=True)


__all__ = ["WhisperXProvider"]
