"""TTS 合成设置（dataclass + 默认值 + 内置音色/语言清单）。

这些设置由 ``TtsSettingsDialog`` 采集，经 controller → worker → provider 透传，
驱动 Qwen3-TTS 的 ``generate_custom_voice`` / ``generate_voice_clone`` 调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# ---------------------------------------------------------------------- #
# 合成模式
# ---------------------------------------------------------------------- #

TtsMode = Literal["custom_voice", "voice_clone"]
"""合成模式：
- ``custom_voice``: 用内置音色（CustomVoice 模型，9 个 speaker + instruct）
- ``voice_clone``:  用参考音频做音色克隆（Base 模型，需 ref_audio + ref_text）
"""


# ---------------------------------------------------------------------- #
# 内置音色（CustomVoice 模型的 9 个 speaker）
# ---------------------------------------------------------------------- #

# (speaker 名, 母语, 性别, 中文说明)
# 名称大小写不敏感（Qwen3-TTS 内部会校验）；这里保留官方大小写便于阅读。
BUILTIN_SPEAKERS: tuple[tuple[str, str, str, str], ...] = (
    ("Vivian",    "Chinese",  "female", "明亮、略带锋芒的年轻女声"),
    ("Serena",    "Chinese",  "female", "温暖、温柔的年轻女声"),
    ("Uncle_Fu",  "Chinese",  "male",   "低沉醇厚的成熟男声"),
    ("Dylan",     "Chinese",  "male",   "清晰自然的北京年轻男声"),
    ("Eric",      "Chinese",  "male",   "成都年轻男声（四川话），略带沙哑"),
    ("Ryan",      "English",  "male",   "节奏感强的活力男声"),
    ("Aiden",     "English",  "male",   "阳光的美式男声，中频清晰"),
    ("Ono_Anna",  "Japanese", "female", "活泼俏皮的日式女声"),
    ("Sohee",     "Korean",   "female", "温暖情感丰富的韩式女声"),
)


# ---------------------------------------------------------------------- #
# 支持语言
# ---------------------------------------------------------------------- #

# (显示名, Qwen3-TTS API 值)；"Auto" 走模型自带的语言自动检测。
BUILTIN_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("自动检测", "Auto"),
    ("中文",    "Chinese"),
    ("英语",    "English"),
    ("日语",    "Japanese"),
    ("韩语",    "Korean"),
    ("德语",    "German"),
    ("法语",    "French"),
    ("俄语",    "Russian"),
    ("葡萄牙语", "Portuguese"),
    ("西班牙语", "Spanish"),
    ("意大利语", "Italian"),
)


# ---------------------------------------------------------------------- #
# TtsSettings
# ---------------------------------------------------------------------- #


@dataclass
class TtsSettings:
    """一次字幕转语音任务的全部可调参数。

    provider 根据 ``mode`` 选择模型与生成方法：
      - ``custom_voice`` → ``generate_custom_voice``（speaker + instruct）
      - ``voice_clone``  → ``generate_voice_clone``（ref_audio + ref_text）
    """

    mode: TtsMode = "custom_voice"

    # --- custom_voice 模式 ---
    speaker: str = "Vivian"
    """CustomVoice 内置音色名（见 :data:`BUILTIN_SPEAKERS`）。"""
    instruct: str = ""
    """自然语言语气/情感指令，如"用愤怒的语气说"。空串表示无指令。"""

    # --- 共享 ---
    language: str = "Auto"
    """合成语言。``"Auto"`` 让模型自动检测（推荐）。"""

    # --- voice_clone 模式 ---
    ref_audio_path: str = ""
    """参考音频文件路径（wav/mp3）。voice_clone 模式必填。"""
    ref_text: str = ""
    """参考音频对应的转写文本。ICL 模式（x_vector_only_mode=False）必填。"""
    x_vector_only_mode: bool = False
    """True = 只用 speaker embedding，无需 ref_text（质量略降但更灵活）。"""

    # --- 高级生成参数（透传给 model.generate_* 的 **kwargs）---
    temperature: float = 0.9
    """采样温度，越大越随机。官方默认 0.9。"""
    top_p: float = 1.0
    """top-p 核采样阈值。官方默认 1.0（等同关闭）。"""
    top_k: int = 50
    """top-k 采样。官方默认 50。"""
    max_new_tokens: int = 2048
    """最大生成的 codec token 数。官方默认 2048。"""

    def to_generate_kwargs(self) -> dict[str, object]:
        """导出为透传给 Qwen3-TTS generate 方法的 kwargs。"""
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "max_new_tokens": self.max_new_tokens,
        }


DEFAULT_TTS_SETTINGS = TtsSettings()
"""对话框打开时的默认设置。"""
