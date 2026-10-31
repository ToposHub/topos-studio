"""各能力的 Protocol 契约（输入/输出类型定义）。

这些 Protocol 定义了"一个能力长什么样"，但不含实现。
实现见 stubs.py（兜底）和后续阶段的 real_*.py（真实模型）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from apps.topos_vault.providers.base import Capability


# ---------------------------------------------------------------------- #
# 结果数据类
# ---------------------------------------------------------------------- #

@dataclass
class EmbeddingResult:
    """向量嵌入结果。"""
    vector: list[float]           # 嵌入向量
    dim: int                      # 维度
    model: str                    # 模型名（用于 vector_index 登记）


@dataclass
class CaptionResult:
    """画面理解结果（视频镜头和图片共用）。"""
    caption: str                  # 自然语言描述
    tags: list[str] = field(default_factory=list)  # 标签数组
    shot_type: str | None = None  # 特写/近景/中景/全景/远景（视频用）
    model: str = ""


@dataclass
class AudioUnderstandingResult:
    """音乐/音频理解结果。"""
    description: str              # 自然语言描述
    mood: str | None = None       # 情绪
    instruments: list[str] = field(default_factory=list)
    vocal_type: str | None = None  # instrumental/vocal/mixed
    # 注：BPM/调性等数值由 librosa 单独算（大模型做不了精确 BPM），不在此结果里
    model: str = ""


@dataclass
class FaceResult:
    """人脸识别结果。"""
    faces: list["SingleFace"] = field(default_factory=list)
    model: str = ""


@dataclass
class SingleFace:
    """单个人脸。"""
    bbox: tuple[int, int, int, int]  # x, y, w, h
    embedding: list[float]            # 512 维人脸向量
    confidence: float
    face_angle: str | None = None     # 正面/侧脸/背面
    completeness: str | None = None   # 完整/遮挡


@dataclass
class TranscriptResult:
    """ASR 转写结果。"""
    lines: list["TranscriptLineData"] = field(default_factory=list)
    model: str = ""


@dataclass
class TranscriptLineData:
    """单句台词。"""
    text: str
    start_sec: float
    end_sec: float
    speaker: str | None = None


# ---------------------------------------------------------------------- #
# 能力 Protocol
# ---------------------------------------------------------------------- #

@runtime_checkable
class TranscriptEmbedProvider(Protocol):
    """能力：台词/文本 → 向量。"""
    provider_id: str
    capability: Capability  # = Capability.TRANSCRIPT_EMBED

    def embed_text(self, text: str) -> EmbeddingResult:
        """把一段文本转向量。"""
        ...

    def embed_texts(self, texts: list[str]) -> list[EmbeddingResult]:
        """批量转（默认实现循环调 embed_text，real 实现可批处理加速）。"""
        ...

    def is_available(self) -> bool: ...


@runtime_checkable
class VisualEmbedProvider(Protocol):
    """能力：画面 → 向量（视频镜头和图片共用）。"""
    provider_id: str
    capability: Capability  # = Capability.VISUAL_EMBED

    def embed_image(self, image_path: str) -> EmbeddingResult:
        """把一张图转向量。"""
        ...

    def is_available(self) -> bool: ...


@runtime_checkable
class VisualCaptionProvider(Protocol):
    """能力：画面 → caption/tags（视频镜头和图片共用）。"""
    provider_id: str
    capability: Capability  # = Capability.VISUAL_CAPTION

    def caption_image(self, image_path: str) -> CaptionResult:
        """理解一张图，生成描述和标签。"""
        ...

    def is_available(self) -> bool: ...


@runtime_checkable
class AudioUnderstandProvider(Protocol):
    """能力：音乐 → 情绪/描述。

    注：BPM/调性等**数值**特征由 librosa 单独算（大模型做不了精确 BPM），
    本 provider 只负责语义理解（情绪/乐器/风格/描述）。
    """
    provider_id: str
    capability: Capability  # = Capability.AUDIO_UNDERSTAND

    def understand_audio(self, audio_path: str) -> AudioUnderstandingResult:
        """理解一段音频。"""
        ...

    def is_available(self) -> bool: ...


@runtime_checkable
class FaceRecognizeProvider(Protocol):
    """能力：画面 → 人脸向量/属性。"""
    provider_id: str
    capability: Capability  # = Capability.FACE_RECOGNIZE

    def detect_faces(self, image_path: str) -> FaceResult:
        """检测图片中的人脸。"""
        ...

    def is_available(self) -> bool: ...


@runtime_checkable
class AsrProvider(Protocol):
    """能力：视频 → 台词（vault 复用主项目 transcript_provider，不重做）。"""
    provider_id: str
    capability: Capability  # = Capability.ASR

    def transcribe(self, audio_path: str) -> TranscriptResult:
        """转写音频为台词。"""
        ...

    def is_available(self) -> bool: ...
