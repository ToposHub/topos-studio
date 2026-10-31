"""Stub provider 实现——无 GPU/无依赖的确定性兜底。

对齐项目主 app 的 ``_stub_transcript`` / ``_stub_ocr`` 模式：按输入的稳定哈希
产固定输出，无网络/无 GPU/无文件依赖。用途：
    - CI/测试（不依赖重模型）
    - 无 GPU 机器上的最小可用流水线
    - 真实 provider 加载失败时的降级兜底

所有 stub 的 ``is_available()`` 永远返回 True（保证总有一个 provider 可用）。
输出用确定性算法（hash 输入 → 固定输出），可复现。
"""

from __future__ import annotations

import hashlib

from apps.topos_vault.providers.base import (
    Capability,
    ProviderDescriptor,
    ProviderRegistry,
    get_registry,
)
from apps.topos_vault.providers.contracts import (
    AudioUnderstandingResult,
    CaptionResult,
    EmbeddingResult,
    FaceResult,
    TranscriptResult,
)


# ---------------------------------------------------------------------- #
# 辅助：确定性哈希（让 stub 输出稳定可复现）
# ---------------------------------------------------------------------- #

def _stable_hash(text: str, length: int = 8) -> str:
    """对输入做稳定哈希（sha256 前 N 位），用于生成确定性 stub 输出。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _hash_to_vector(seed: str, dim: int) -> list[float]:
    """从 seed 生成确定性的伪向量（用于 stub 的 embedding）。

    不是真实语义向量，只是维度正确、稳定可复现的占位。
    """
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    # 循环扩展 hash 到 dim 长度
    vec = []
    for i in range(dim):
        byte = h[i % len(h)]
        # 映射到 [-1, 1] 区间
        vec.append((byte / 127.5) - 1.0)
    return vec


# ---------------------------------------------------------------------- #
# Stub 实现
# ---------------------------------------------------------------------- #

class StubTranscriptEmbedProvider:
    """transcript_embed 的 stub。"""
    provider_id = "stub_transcript_embed"
    capability = Capability.TRANSCRIPT_EMBED

    def embed_text(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(
            vector=_hash_to_vector(text, 1024), dim=1024, model=self.provider_id,
        )

    def embed_texts(self, texts: list[str]) -> list[EmbeddingResult]:
        return [self.embed_text(t) for t in texts]

    def is_available(self) -> bool:
        return True


class StubVisualEmbedProvider:
    """visual_embed 的 stub。"""
    provider_id = "stub_visual_embed"
    capability = Capability.VISUAL_EMBED

    def embed_image(self, image_path: str) -> EmbeddingResult:
        # 维度对齐 SigLIP2 base（768）；get_vector_store 会按实际 provider 维度建库
        return EmbeddingResult(
            vector=_hash_to_vector(image_path, 768), dim=768, model=self.provider_id,
        )

    def is_available(self) -> bool:
        return True


class StubVisualCaptionProvider:
    """visual_caption 的 stub。"""
    provider_id = "stub_visual_caption"
    capability = Capability.VISUAL_CAPTION

    def caption_image(self, image_path: str) -> CaptionResult:
        # 确定性输出：根据文件名 hash 选标签
        h = _stable_hash(image_path)
        all_tags = ["室内", "室外", "夜景", "人像", "风景", "特写", "全景"]
        tags = [all_tags[int(h[i], 16) % len(all_tags)] for i in range(2)]
        return CaptionResult(
            caption=f"[stub] 确定性描述（{h}）",
            tags=list(dict.fromkeys(tags)),  # 去重保序
            shot_type="中景",
            model=self.provider_id,
        )

    def is_available(self) -> bool:
        return True


class StubAudioUnderstandProvider:
    """audio_understand 的 stub。"""
    provider_id = "stub_audio_understand"
    capability = Capability.AUDIO_UNDERSTAND

    def understand_audio(self, audio_path: str) -> AudioUnderstandingResult:
        h = _stable_hash(audio_path)
        moods = ["happy", "sad", "tense", "calm", "energetic"]
        return AudioUnderstandingResult(
            description=f"[stub] 确定性音乐描述（{h}）",
            mood=moods[int(h[0], 16) % len(moods)],
            instruments=["strings", "percussion"],
            vocal_type="mixed",
            model=self.provider_id,
        )

    def is_available(self) -> bool:
        return True


class StubFaceRecognizeProvider:
    """face_recognize 的 stub。"""
    provider_id = "stub_face_recognize"
    capability = Capability.FACE_RECOGNIZE

    def detect_faces(self, image_path: str) -> FaceResult:
        # stub：返回 0 个人脸（空结果，确定性）
        return FaceResult(faces=[], model=self.provider_id)

    def is_available(self) -> bool:
        return True


class StubAsrProvider:
    """asr 的 stub。"""
    provider_id = "stub_asr"
    capability = Capability.ASR

    def transcribe(self, audio_path: str) -> TranscriptResult:
        # stub：返回空台词
        return TranscriptResult(lines=[], model=self.provider_id)

    def is_available(self) -> bool:
        return True


# ---------------------------------------------------------------------- #
# 注册所有 stub（默认 provider，真实实现注册后覆盖默认）
# ---------------------------------------------------------------------- #

_STUB_DESCRIPTORS = {
    "stub_transcript_embed": ProviderDescriptor(
        provider_id="stub_transcript_embed",
        capability=Capability.TRANSCRIPT_EMBED,
        display_name="Stub 台词向量（无 GPU）",
        requires_gpu=False, min_vram_mb=0,
        notes="确定性兜底，无语义能力，仅用于测试/CI",
    ),
    "stub_visual_embed": ProviderDescriptor(
        provider_id="stub_visual_embed",
        capability=Capability.VISUAL_EMBED,
        display_name="Stub 画面向量（无 GPU）",
        requires_gpu=False, min_vram_mb=0,
        notes="确定性兜底",
    ),
    "stub_visual_caption": ProviderDescriptor(
        provider_id="stub_visual_caption",
        capability=Capability.VISUAL_CAPTION,
        display_name="Stub 画面理解（无 GPU）",
        requires_gpu=False, min_vram_mb=0,
        notes="确定性兜底",
    ),
    "stub_audio_understand": ProviderDescriptor(
        provider_id="stub_audio_understand",
        capability=Capability.AUDIO_UNDERSTAND,
        display_name="Stub 音频理解（无 GPU）",
        requires_gpu=False, min_vram_mb=0,
        notes="确定性兜底",
    ),
    "stub_face_recognize": ProviderDescriptor(
        provider_id="stub_face_recognize",
        capability=Capability.FACE_RECOGNIZE,
        display_name="Stub 人脸识别（无 GPU）",
        requires_gpu=False, min_vram_mb=0,
        notes="确定性兜底，返回空人脸",
    ),
    "stub_asr": ProviderDescriptor(
        provider_id="stub_asr",
        capability=Capability.ASR,
        display_name="Stub ASR（无 GPU）",
        requires_gpu=False, min_vram_mb=0,
        notes="确定性兜底，返回空台词。真实 ASR 复用主项目 transcript_provider",
    ),
}

_STUB_PROVIDERS = [
    StubTranscriptEmbedProvider(),
    StubVisualEmbedProvider(),
    StubVisualCaptionProvider(),
    StubAudioUnderstandProvider(),
    StubFaceRecognizeProvider(),
    StubAsrProvider(),
]


def register_stubs(registry: ProviderRegistry | None = None) -> None:
    """把所有 stub provider 注册为默认（真实 provider 注册时覆盖）。"""
    reg = registry or get_registry()
    for provider in _STUB_PROVIDERS:
        desc = _STUB_DESCRIPTORS[provider.provider_id]
        # 如果已注册（重复 import），跳过
        if reg.get(provider.provider_id) is None:
            reg.register(provider, desc, is_default=True)


# 模块导入时自动注册（与项目 PanelRegistry 单例模式一致）
register_stubs()
