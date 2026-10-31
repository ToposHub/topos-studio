"""TAEP AssetAnalysis 模型与 AnalysisProvider 契约（Phase 5）。

素材分析是独立于剪辑计划的**缓存数据**（TAEP §5.4）。为每个 ``asset_id`` 建立版本化
``AssetAnalysis``，把不同 provider 的结果归一为可查询的 ``SemanticSegment``。

规则（TAEP §5.4 / §12 P5）：

- 分析结果只能提出剪辑候选，不能直接改变工程。
- ``fingerprint`` 不匹配即失效（素材被替换/修改后分析过期）。
- 用户手工标注与 AI 推断分层保存；用户修订不被重新分析覆盖。
- provider 不可直接写 timeline；输出经 ``to_semantic_segments`` 转换后入索引。
- 模型、提示词和转录文本可能属于敏感数据；云 provider 上传必须单独用户确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from src.features.edit.ai_editing.editorial_models import FrameRange, SegmentKind, SemanticSegment


# ---------------------------------------------------------------------- #
# AssetFingerprint（素材指纹，用于失效判定）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class AssetFingerprint:
    """素材指纹：用于判断分析是否过期（TAEP §5.4 ``asset_fingerprint``）。

    ``content_hash`` 可选（计算昂贵）；``size`` + ``mtime_ns`` 是廉价的初步判定。
    """

    size: int = 0
    mtime_ns: int = 0
    content_hash: str | None = None

    def matches(self, other: "AssetFingerprint") -> bool:
        """判断指纹是否一致（content_hash 优先；否则 size+mtime）。"""
        if self.content_hash and other.content_hash:
            return self.content_hash == other.content_hash
        return self.size == other.size and self.mtime_ns == other.mtime_ns

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"size": self.size, "mtime_ns": self.mtime_ns}
        if self.content_hash is not None:
            d["content_hash"] = self.content_hash
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AssetFingerprint":
        return AssetFingerprint(
            size=int(data.get("size", 0)),
            mtime_ns=int(data.get("mtime_ns", 0)),
            content_hash=data.get("content_hash"),
        )


# ---------------------------------------------------------------------- #
# AssetAnalysis（版本化的分析聚合）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class WordSegment:
    """词级时间码（WhisperX word alignment；TAEP §5.4 ``transcript.words``）。

    M4：让"按词搜索/定位"成为可能。``start_frame``/``end_frame`` 半开区间，
    经 :class:`TimebaseService` 从 provider PTS/sample 整数换算（绝不用秒 × fps）。
    """

    start_frame: int
    end_frame: int  # 半开区间右端
    text: str
    confidence: float = 1.0

    def to_frame_range(self) -> FrameRange:
        return FrameRange(self.start_frame, max(1, self.end_frame - self.start_frame))

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "text": self.text,
            "confidence": self.confidence,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "WordSegment":
        return WordSegment(
            start_frame=int(data.get("start_frame", 0)),
            end_frame=int(data.get("end_frame", 0)),
            text=str(data.get("text", "")),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass(frozen=True)
class TranscriptSegment:
    """单条转录片段（TAEP §5.4 ``transcript.segments``）。

    ``speaker`` 仅允许匿名标签（``S1``/``S2``/...）；diarization 不得输出实名身份
    （§11.1 M4 红线：把 speaker 聚类当实名身份）。``words`` 为可选词级时间码，
    缺失对齐模型时为空并经 ``confidence`` 下调标记精度（不静默降级）。
    """

    start_frame: int
    end_frame: int  # 半开区间右端
    text: str
    speaker: str | None = None
    confidence: float = 1.0
    language: str | None = None
    # M4：词级时间码（WhisperX word alignment）。无对齐模型的语言此字段为空，
    # 且 confidence 下调（见 WhisperXTranscriptProvider 降级策略）。
    words: tuple[WordSegment, ...] = ()

    def to_frame_range(self) -> FrameRange:
        return FrameRange(self.start_frame, max(1, self.end_frame - self.start_frame))

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "text": self.text,
            "confidence": self.confidence,
        }
        if self.speaker is not None:
            d["speaker"] = self.speaker
        if self.language is not None:
            d["language"] = self.language
        if self.words:
            d["words"] = [w.to_dict() for w in self.words]
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TranscriptSegment":
        words = tuple(
            WordSegment.from_dict(w) for w in (data.get("words") or ())
        )
        return TranscriptSegment(
            start_frame=int(data.get("start_frame", 0)),
            end_frame=int(data.get("end_frame", 0)),
            text=str(data.get("text", "")),
            speaker=data.get("speaker"),
            confidence=float(data.get("confidence", 1.0)),
            language=data.get("language"),
            words=words,
        )


@dataclass(frozen=True)
class AudioAnalysis:
    """音频分析摘要（TAEP §5.4 ``audio``）。"""

    integrated_lufs: float | None = None
    speech_ranges: tuple[tuple[int, int], ...] = ()  # [(start, end), ...]
    bpm: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.integrated_lufs is not None:
            d["integrated_lufs"] = self.integrated_lufs
        if self.speech_ranges:
            d["speech_ranges"] = [list(r) for r in self.speech_ranges]
        if self.bpm is not None:
            d["bpm"] = self.bpm
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AudioAnalysis":
        return AudioAnalysis(
            integrated_lufs=(
                float(data["integrated_lufs"])
                if data.get("integrated_lufs") is not None else None
            ),
            speech_ranges=tuple(
                (int(item[0]), int(item[1]))
                for item in (data.get("speech_ranges") or ())
                if isinstance(item, (list, tuple)) and len(item) >= 2
            ),
            bpm=float(data["bpm"]) if data.get("bpm") is not None else None,
        )


@dataclass(frozen=True)
class OcrBox:
    """单个 OCR 检测框（归一化坐标 0..1，与帧分辨率解耦，§18.1）。

    坐标系：左上角原点，``x``/``y`` 为框左上角，``w``/``h`` 为宽高，均归一化到
    ``[0.0, 1.0]``。归一化使检测结果不随帧分辨率变化（缩略图/代理/4K 一致）。
    """

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        for name, val in (("x", self.x), ("y", self.y),
                          ("w", self.w), ("h", self.h)):
            if not 0.0 <= val <= 1.0:
                raise ValueError(f"OcrBox.{name} must be in [0.0, 1.0], got {val}")

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "OcrBox":
        return OcrBox(
            x=float(data.get("x", 0.0)),
            y=float(data.get("y", 0.0)),
            w=float(data.get("w", 0.0)),
            h=float(data.get("h", 0.0)),
        )


@dataclass(frozen=True)
class OcrDetection:
    """单帧上的一处 OCR 检测（§18.1 RapidOCR adapter 输出）。

    ``frame`` 为 source frame（整帧，由关键帧抽样决定，非 PTS 换算）。``text`` 为
    识别文本——**不可信数据（§9）**：经 ``sanitize_untrusted`` 标记后才能进模型上下文，
    不能改变系统指令/权限，也不能未经用户确认进字幕。
    """

    frame: int
    text: str
    confidence: float = 1.0
    language: str | None = None
    box: OcrBox | None = None

    def __post_init__(self) -> None:
        if self.frame < 0:
            raise ValueError(f"OcrDetection.frame must be >= 0, got {self.frame}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"OcrDetection.confidence must be in [0.0, 1.0], got {self.confidence}"
            )

    def to_frame_range(self) -> FrameRange:
        """点事件：``[frame, frame + 1)``。"""
        return FrameRange(self.frame, 1)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "frame": self.frame,
            "text": self.text,
            "confidence": self.confidence,
        }
        if self.language is not None:
            d["language"] = self.language
        if self.box is not None:
            d["box"] = self.box.to_dict()
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "OcrDetection":
        box_raw = data.get("box")
        return OcrDetection(
            frame=int(data.get("frame", 0)),
            text=str(data.get("text", "")),
            confidence=float(data.get("confidence", 1.0)),
            language=data.get("language"),
            box=OcrBox.from_dict(box_raw) if box_raw else None,
        )


@dataclass(frozen=True)
class AnalysisProvenance:
    """分析的来源与生成信息（TAEP §5.4 ``generator``）。

    M2 升级（§18.2）：``AssetAnalysis`` 现可携带 ``FieldProvenance``（每个字段独立来源），
    本类仍是单字段的来源记录，便于"转录来自 ASR vX，OCR 来自 RapidOCR vY"独立更新。
    """

    provider: str = "local"
    model: str = ""
    created_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"provider": self.provider, "model": self.model, "created_at": self.created_at}
        if self.extra:
            d["extra"] = dict(self.extra)
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AnalysisProvenance":
        return AnalysisProvenance(
            provider=str(data.get("provider", "local")),
            model=str(data.get("model", "")),
            created_at=str(data.get("created_at", "")),
            extra=dict(data.get("extra", {})),
        )


# AssetAnalysis 的分析字段名集合（FieldProvenance 按 name 索引用）
_ANALYSIS_FIELDS: tuple[str, ...] = ("scene_cuts", "transcript", "tags", "audio", "ocr")


@dataclass(frozen=True)
class FieldProvenance:
    """每个分析字段独立的来源（M2 / §18.2 field-level provenance）。

    解决旧 ``AssetAnalysis.provenance``（单条全局）的问题：转录来自本地 ASR vX，
    OCR 来自 RapidOCR vY，响度来自 FFmpeg vZ——它们应可**独立更新**，且绝不互相覆盖。

    ``AssetAnalysis.field_provenance`` 携带本容器；``None`` 时回退到全局 ``provenance``
    （向后兼容旧 payload）。
    """

    scene_cuts: AnalysisProvenance | None = None
    transcript: AnalysisProvenance | None = None
    tags: AnalysisProvenance | None = None
    audio: AnalysisProvenance | None = None
    ocr: AnalysisProvenance | None = None

    def for_field(self, name: str) -> AnalysisProvenance | None:
        """按字段名取来源（``name`` ∈ scene_cuts/transcript/tags/audio/ocr）。"""
        return getattr(self, name, None)

    def with_field(self, name: str, provenance: AnalysisProvenance) -> "FieldProvenance":
        """返回一份把指定字段来源更新后的副本（不可变转换）。"""
        if name not in _ANALYSIS_FIELDS:
            raise ValueError(f"unknown analysis field {name!r}; expected one of {_ANALYSIS_FIELDS}")
        from dataclasses import replace
        return replace(self, **{name: provenance})

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for name in _ANALYSIS_FIELDS:
            p = getattr(self, name, None)
            if p is not None:
                d[name] = p.to_dict()
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "FieldProvenance":
        kw: dict[str, Any] = {}
        for name in _ANALYSIS_FIELDS:
            if name in data and data[name]:
                kw[name] = AnalysisProvenance.from_dict(data[name])
        return FieldProvenance(**kw)

    @staticmethod
    def single(name: str, provenance: AnalysisProvenance) -> "FieldProvenance":
        """便捷构造：只填一个字段的来源。"""
        return FieldProvenance().with_field(name, provenance)


@dataclass(frozen=True)
class AssetAnalysis:
    """单个素材的完整分析聚合（TAEP §5.4 + M2 field-level provenance）。

    各字段独立版本化；scene_cuts/transcript/tags/audio/ocr 由不同 provider 填充。
    ``fingerprint`` 不匹配时整个分析失效。

    M2（§18.2）：``field_provenance`` 按字段独立记录来源，使"转录来自 ASR vX，OCR 来自
    RapidOCR vY"可独立更新；``None`` 时回退到全局 ``provenance``（向后兼容旧 payload）。
    """

    asset_id: str
    fingerprint: AssetFingerprint
    fps: str = "24/1"  # 分析时所基于的帧率（Rational 字符串）
    scene_cuts: tuple[dict[str, Any], ...] = ()  # [{frame, confidence, kind}]
    transcript: tuple[TranscriptSegment, ...] = ()
    tags: tuple[dict[str, Any], ...] = ()  # [{start_frame, end_frame, labels}]
    ocr: tuple[OcrDetection, ...] = ()  # M8: [{frame, text, confidence, language, box}]
    audio: AudioAnalysis | None = None
    provenance: AnalysisProvenance = field(default_factory=AnalysisProvenance)
    field_provenance: FieldProvenance | None = None  # M2: 字段级来源（优先于全局 provenance）
    user_verified: bool = False  # 用户是否确认过整体分析

    def is_valid_for(self, current_fingerprint: AssetFingerprint) -> bool:
        """指纹是否仍匹配（素材未被替换/修改）。"""
        return self.fingerprint.matches(current_fingerprint)

    def provenance_for(self, field_name: str) -> AnalysisProvenance:
        """取某字段的来源：优先 field_provenance，回退全局 provenance。

        ``analysis_to_segments`` 用本方法给每个 segment 标注来源，使 transcript segment
        携带 ASR 来源、shot segment 携带 scene_cut 来源，而非统一全局来源。
        """
        if self.field_provenance is not None:
            p = self.field_provenance.for_field(field_name)
            if p is not None:
                return p
        return self.provenance

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "asset_id": self.asset_id,
            "fingerprint": self.fingerprint.to_dict(),
            "fps": self.fps,
            "scene_cuts": [dict(c) for c in self.scene_cuts],
            "transcript": [t.to_dict() for t in self.transcript],
            "tags": [dict(t) for t in self.tags],
            "ocr": [o.to_dict() for o in self.ocr],
            "provenance": self.provenance.to_dict(),
            "user_verified": self.user_verified,
        }
        if self.audio is not None:
            d["audio"] = self.audio.to_dict()
        if self.field_provenance is not None:
            d["field_provenance"] = self.field_provenance.to_dict()
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AssetAnalysis":
        """Restore an analysis cache entry without invoking any provider."""
        audio = data.get("audio")
        field_provenance = data.get("field_provenance")
        return AssetAnalysis(
            asset_id=str(data.get("asset_id", "")),
            fingerprint=AssetFingerprint.from_dict(dict(data.get("fingerprint", {}))),
            fps=str(data.get("fps", "24/1")),
            scene_cuts=tuple(dict(item) for item in (data.get("scene_cuts") or ())),
            transcript=tuple(
                TranscriptSegment.from_dict(dict(item))
                for item in (data.get("transcript") or ())
            ),
            tags=tuple(dict(item) for item in (data.get("tags") or ())),
            ocr=tuple(
                OcrDetection.from_dict(dict(item))
                for item in (data.get("ocr") or ())
            ),
            audio=AudioAnalysis.from_dict(dict(audio)) if isinstance(audio, dict) else None,
            provenance=AnalysisProvenance.from_dict(dict(data.get("provenance", {}))),
            field_provenance=(
                FieldProvenance.from_dict(dict(field_provenance))
                if isinstance(field_provenance, dict) else None
            ),
            user_verified=bool(data.get("user_verified", False)),
        )


# ---------------------------------------------------------------------- #
# AnalysisProvider 契约（TAEP §12 P5）
# ---------------------------------------------------------------------- #


class AnalysisProvider(Protocol):
    """可插拔的素材分析 provider 契约。

    provider 不可直接写 timeline；输出经 ``to_semantic_segments`` 归一后入索引。
    云 provider 的上传必须由调用方单独获得用户确认（TAEP §12 P5）。
    """

    provider_id: str
    """provider 稳定标识（如 'scene_cut_local' / 'transcript_cloud'）。"""

    supported_kinds: tuple[SegmentKind, ...]
    """该 provider 产出的 segment 类型。"""

    def analyze(
        self, asset_id: str, fingerprint: AssetFingerprint, *, fps: str,
        progress: Any | None = None,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        """执行分析，返回 (AssetAnalysis 片段, SemanticSegments)。

        返回的 AssetAnalysis 只填该 provider 负责的字段；聚合由 AnalysisService 做。
        """
        ...


# ---------------------------------------------------------------------- #
# AssetAnalysis → SemanticSegment 转换（归一化）
# ---------------------------------------------------------------------- #


def analysis_to_segments(analysis: AssetAnalysis) -> list[SemanticSegment]:
    """把 AssetAnalysis 各字段归一为 SemanticSegment 列表（TAEP §5.4 + M2 field provenance）。

    M2（§18.2）：每个 segment 的 ``provenance`` 按其字段来源标注（shot=scene_cuts，
    transcript=transcript，tag=tags，audio=audio），经 ``AssetAnalysis.provenance_for``；
    ``field_provenance`` 为 None 时回退到全局 ``provenance``（向后兼容）。

    provider 的原始标签、模型版本和置信度保留在 ``provenance``，不能被规划器
    伪装成用户事实（``user_verified`` 分层）。
    """
    import uuid as _uuid

    def _prov_for(field_name: str) -> dict[str, Any]:
        p = analysis.provenance_for(field_name)
        return {"provider": p.provider, "model": p.model}

    segments: list[SemanticSegment] = []

    # scene cuts → SHOT segments（每个 cut 标记一个镜头边界）
    for cut in analysis.scene_cuts:
        frame = int(cut.get("frame", 0))
        confidence = float(cut.get("confidence", 1.0))
        # scene cut 是边界点；用 [frame, 1) 表示一个边界事件
        segments.append(SemanticSegment(
            segment_id=f"shot-{analysis.asset_id}-{frame}-{_uuid.uuid4().hex[:8]}",
            asset_id=analysis.asset_id,
            source_range=FrameRange(frame, 1),
            kind=SegmentKind.SHOT,
            payload={"kind_detail": cut.get("kind", "cut"), "boundary": True},
            confidence=confidence,
            provenance=_prov_for("scene_cuts"),
            user_verified=analysis.user_verified,
        ))

    # transcript → TRANSCRIPT segments
    for seg in analysis.transcript:
        # M4：词级时间码随 segment 入索引，供"按词搜索/定位"。空 words 不进 payload
        # （保持旧 payload 形状，向后兼容）。
        payload: dict[str, Any] = {
            "text": seg.text, "speaker": seg.speaker, "language": seg.language,
        }
        if seg.words:
            payload["words"] = [
                {"start_frame": w.start_frame, "end_frame": w.end_frame,
                 "text": w.text, "confidence": w.confidence}
                for w in seg.words
            ]
        segments.append(SemanticSegment(
            segment_id=f"transcript-{analysis.asset_id}-{seg.start_frame}-{_uuid.uuid4().hex[:8]}",
            asset_id=analysis.asset_id,
            source_range=seg.to_frame_range(),
            kind=SegmentKind.TRANSCRIPT,
            payload=payload,
            confidence=seg.confidence,
            provenance=_prov_for("transcript"),
            user_verified=analysis.user_verified,
        ))

    # tags → TAG segments
    for tag in analysis.tags:
        start = int(tag.get("start_frame", 0))
        end = int(tag.get("end_frame", start + 1))
        labels = tag.get("labels", [])
        segments.append(SemanticSegment(
            segment_id=f"tag-{analysis.asset_id}-{start}-{_uuid.uuid4().hex[:8]}",
            asset_id=analysis.asset_id,
            source_range=FrameRange(start, max(1, end - start)),
            kind=SegmentKind.TAG,
            payload={"labels": list(labels) if isinstance(labels, (list, tuple)) else [labels]},
            confidence=float(tag.get("confidence", 1.0)),
            provenance=_prov_for("tags"),
            user_verified=analysis.user_verified,
        ))

    # ocr → OCR segments（M8：每个检测是一个点事件 [frame, frame+1)）
    # OCR 文本是 untrusted data（§9）；payload.text 使 _segment_matches_label 能命中
    # （"找屏幕文字"立即可用）。经 sanitize_untrusted 在 tool_facade 层标记为不可信。
    for det in analysis.ocr:
        payload_ocr: dict[str, Any] = {"text": det.text}
        if det.language is not None:
            payload_ocr["language"] = det.language
        if det.box is not None:
            payload_ocr["box"] = det.box.to_dict()
        segments.append(SemanticSegment(
            segment_id=f"ocr-{analysis.asset_id}-{det.frame}-{_uuid.uuid4().hex[:8]}",
            asset_id=analysis.asset_id,
            source_range=FrameRange(det.frame, 1),
            kind=SegmentKind.OCR,
            payload=payload_ocr,
            confidence=det.confidence,
            provenance=_prov_for("ocr"),
            user_verified=analysis.user_verified,
        ))

    # audio speech ranges → AUDIO segments
    if analysis.audio and analysis.audio.speech_ranges:
        for start, end in analysis.audio.speech_ranges:
            segments.append(SemanticSegment(
                segment_id=f"audio-{analysis.asset_id}-{start}-{_uuid.uuid4().hex[:8]}",
                asset_id=analysis.asset_id,
                source_range=FrameRange(start, max(1, end - start)),
                kind=SegmentKind.AUDIO,
                payload={"speech": True, "integrated_lufs": analysis.audio.integrated_lufs,
                         "bpm": analysis.audio.bpm},
                confidence=1.0,
                provenance=_prov_for("audio"),
                user_verified=analysis.user_verified,
            ))

    return segments
