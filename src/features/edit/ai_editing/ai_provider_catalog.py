"""AI provider sources, capability groups and safe built-in presets.

The preferences page uses this catalog as its single source of truth for the
human-facing provider choices.  A preset contains no credential; it only
pre-fills the protocol, endpoint and model so the user can enter a key in the
secure credential store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

from src.features.edit.ai_editing.mcp_vision_gateway import (
    ZHIPU_VISION_ORIGIN, ZHIPU_VISION_PROFILE_ID, ZHIPU_VISION_PROVIDER_KIND, ZHIPU_VISION_TOOL,
)


SOURCE_LOCAL = "local"
SOURCE_OLLAMA = "ollama"
SOURCE_PRESET = "preset"
SOURCE_CUSTOM = "custom"

SOURCE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("本机模型", SOURCE_LOCAL),
    ("Ollama 服务", SOURCE_OLLAMA),
    ("第三方服务预设", SOURCE_PRESET),
    ("自定义 API", SOURCE_CUSTOM),
)


@dataclass(frozen=True)
class AiProviderPreset:
    """A non-secret provider template shown in Preferences."""

    preset_id: str
    label: str
    source_kind: str
    provider_kind: str
    endpoint: str
    model_id: str
    capabilities: tuple[str, ...]
    modalities: tuple[str, ...] = ()
    description: str = ""
    label_en: str = ""
    description_en: str = ""


# Keep this list deliberately small and explicit.  It is configuration UI,
# not a vendor SDK registry.  New providers can always use the Custom API row.
PROVIDER_PRESETS: tuple[AiProviderPreset, ...] = (
    AiProviderPreset(
        "local-openai-compatible",
        "本地模型 · OpenAI 兼容服务",
        SOURCE_LOCAL,
        "local_http",
        "http://127.0.0.1:8000/v1",
        "qwen2.5-7b-instruct",
        ("language",),
        ("text", "tool_call"),
        "适用于本机 vLLM / llama.cpp / LM Studio 等 OpenAI 兼容服务",
        label_en="Local model · OpenAI-compatible server",
        description_en="Connect to a local OpenAI-compatible server such as vLLM, llama.cpp, or LM Studio.",
    ),
    AiProviderPreset(
        "openai-compatible-vision",
        "自建服务 · OpenAI 兼容视觉",
        SOURCE_CUSTOM,
        "openai",
        "http://127.0.0.1:8000/v1",
        "",
        ("vision",),
        ("image",),
        "可填写局域网服务器地址，再获取或输入视觉模型 ID",
        label_en="Self-hosted · OpenAI-compatible vision",
        description_en="Enter the local network server URL, then discover or enter a vision model ID.",
    ),
    AiProviderPreset(
        ZHIPU_VISION_PROFILE_ID,
        "智谱 · 视觉 MCP",
        SOURCE_PRESET,
        ZHIPU_VISION_PROVIDER_KIND,
        ZHIPU_VISION_ORIGIN,
        ZHIPU_VISION_TOOL,
        ("vision",),
        ("image",),
        "智谱 GLM Coding Plan · npx 启动官方 stdio 服务，需要 Node.js 18+",
        label_en="Zhipu · Vision MCP",
        description_en="Zhipu GLM Coding Plan. Launches the official stdio server via npx and requires Node.js 18+.",
    ),
    AiProviderPreset(
        "model-studio-vision",
        "阿里云百炼 · 千问视觉",
        SOURCE_PRESET,
        "openai",
        "",
        "",
        ("vision",),
        ("image",),
        "从百炼控制台复制当前工作空间的 OpenAI 兼容地址和视觉模型 ID",
        label_en="Alibaba Cloud Model Studio · Qwen Vision",
        description_en="Copy the OpenAI-compatible URL and vision model ID for your workspace from the Model Studio console.",
    ),
    AiProviderPreset(
        "openai-timed-asr",
        "OpenAI · 带时间码的转录",
        SOURCE_PRESET,
        "openai",
        "https://api.openai.com/v1",
        "whisper-1",
        ("asr",),
        ("asr",),
        "OpenAI 兼容转录接口；自建服务可修改地址和模型 ID",
        label_en="OpenAI · Timestamped transcription",
        description_en="OpenAI-compatible transcription API. For self-hosted services, edit the URL and model ID.",
    ),
    AiProviderPreset(
        "openai-chat",
        "OpenAI · Chat",
        SOURCE_PRESET,
        "openai",
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        ("language",),
        ("text", "tool_call"),
        "OpenAI 兼容聊天接口",
        description_en="OpenAI-compatible chat API.",
    ),
    AiProviderPreset(
        "anthropic-chat",
        "Anthropic · Claude",
        SOURCE_PRESET,
        "anthropic",
        "https://api.anthropic.com",
        "claude-3-5-sonnet-latest",
        ("language",),
        ("text", "tool_call"),
        "Anthropic Messages 接口",
        description_en="Anthropic Messages API.",
    ),
    AiProviderPreset(
        "deepseek-flash",
        "DeepSeek · Flash",
        SOURCE_PRESET,
        "deepseek",
        "https://api.deepseek.com",
        "deepseek-flash",
        ("language", "vision_understanding"),
        ("text", "tool_call", "image"),
        "DeepSeek OpenAI 兼容接口，支持对话、工具调用和图片理解",
        description_en="DeepSeek’s OpenAI-compatible API for chat, tool calls, and image understanding.",
    ),
    AiProviderPreset(
        "deepseek-chat",
        "DeepSeek · Chat",
        SOURCE_PRESET,
        "deepseek",
        "https://api.deepseek.com",
        "deepseek-chat",
        ("language",),
        ("text", "tool_call"),
        "DeepSeek OpenAI 兼容接口",
        description_en="DeepSeek’s OpenAI-compatible API.",
    ),
    AiProviderPreset(
        "zhipu-glm-coding",
        "智谱 GLM Coding",
        SOURCE_PRESET,
        "openai",
        "https://open.bigmodel.cn/api/coding/paas/v4",
        "glm-5.2",
        # 注意：glm-5.2 经 Coding 端点实测只接受 text content（带图会返回
        # HTTP 400 "messages.content.type 参数非法，取值范围 ['text']"），
        # 因此不声明 vision_understanding。多模态需用 GLM-4V 系列/常规端点。
        ("language",),
        ("text", "tool_call"),
        "GLM Coding Plan；支持填写 Base URL 或完整 /chat/completions 地址",
        label_en="Zhipu GLM Coding",
        description_en="GLM Coding Plan. Accepts a base URL or a complete /chat/completions URL.",
    ),
    AiProviderPreset(
        "volcengine-agent-plan-text",
        "火山方舟 Agent Plan · 全模态",
        SOURCE_PRESET,
        "volcengine_responses",
        "https://ark.cn-beijing.volces.com/api/plan/v3",
        "doubao-seed-2.0-lite",
        # Seed 2.0 Lite 的 Agent Plan 版本支持文本、图片、视频、音频与文件输入。
        # 具体可用格式仍以账号所绑定的模型版本/套餐为准；transport 会在发送前
        # 将本地文件上传到 File API，再使用 file_id 引用。
        (
            "language", "vision_understanding", "video_understanding",
            "audio_understanding", "document_understanding", "file_input",
        ),
        (
            "text", "tool_call", "image", "video", "audio", "file",
            "media_upload",
        ),
        "Agent Plan Responses API；支持图片、视频、音频、PDF/文件输入；API Key 单独安全保存",
        label_en="Volcengine Ark Agent Plan · Multimodal",
        description_en="Agent Plan Responses API for images, video, audio, PDFs, and files. API keys are stored separately.",
    ),
    AiProviderPreset(
        "volcengine-seedream-image",
        "火山方舟 Agent Plan · 图片",
        SOURCE_PRESET,
        "volcengine_seedream",
        "https://ark.cn-beijing.volces.com/api/plan/v3",
        "doubao-seedream-5.0-lite",
        ("image",),
        ("image",),
        "Seedream 图片生成；Medium 套餐可用能力以控制台为准",
        label_en="Volcengine Ark Agent Plan · Images",
        description_en="Seedream image generation. Check the console for capabilities included in the Medium plan.",
    ),
    AiProviderPreset(
        "volcengine-plan-tts",
        "火山方舟 Agent Plan · 语音合成",
        SOURCE_PRESET,
        "volcengine_plan_tts",
        "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional",
        "doubao-seed-tts-2.0",
        ("tts",),
        ("voice",),
        "豆包语音合成 HTTP 接口；Resource-Id 由适配器使用",
        label_en="Volcengine Ark Agent Plan · Speech synthesis",
        description_en="Doubao speech synthesis over HTTP. The adapter uses the Resource-Id.",
    ),
    AiProviderPreset(
        "volcengine-plan-asr",
        "火山方舟 Agent Plan · 语音识别",
        SOURCE_PRESET,
        "volcengine_plan_asr",
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
        "volc.bigasr.auc_turbo",
        ("asr",),
        (),
        "文件识别端点；Resource-Id 可按控制台实际开通资源调整",
        label_en="Volcengine Ark Agent Plan · Speech recognition",
        description_en="File transcription endpoint. Adjust the Resource-Id to match the resources enabled in the console.",
    ),
    AiProviderPreset(
        "local-openai-compatible-embedding",
        "本机 · OpenAI 兼容向量",
        SOURCE_LOCAL,
        "local_http",
        "http://127.0.0.1:8000/v1",
        "",
        ("embedding",),
        description="连接本机 OpenAI 兼容向量服务，可在模型列表中选择模型。",
        label_en="Local · OpenAI-compatible embeddings",
        description_en="Connect to a local OpenAI-compatible embedding server and select an available model.",
    ),
    AiProviderPreset(
        "ollama-embedding",
        "Ollama · 本机向量",
        SOURCE_OLLAMA,
        "ollama",
        "http://127.0.0.1:11434/api/embed",
        "",
        ("embedding",),
        description="使用 Ollama 原生向量接口，可选择已安装的嵌入模型。",
        label_en="Ollama · Local embeddings",
        description_en="Use Ollama's native embedding API and select an installed embedding model.",
    ),
    AiProviderPreset(
        "openai-embedding",
        "OpenAI · Embeddings",
        SOURCE_PRESET,
        "openai",
        "https://api.openai.com/v1",
        "text-embedding-3-small",
        ("embedding",),
        (),
        "使用 OpenAI 向量模型为素材库提供语义搜索。",
        description_en="Use OpenAI embeddings for semantic library search.",
    ),
    AiProviderPreset(
        "local-qwen-asr",
        "本地 Qwen3-ASR",
        SOURCE_LOCAL,
        "local_qwen_asr",
        "",
        "qwen3_asr",
        ("asr",),
        (),
        "读取本机模型包，不上传素材",
        label_en="Local Qwen3-ASR",
        description_en="Uses the model pack on this device. Media stays on this device.",
    ),
    AiProviderPreset(
        "local-qwen-tts",
        "本地 Qwen3-TTS",
        SOURCE_LOCAL,
        "local_qwen_tts",
        "",
        "qwen3_tts_customvoice",
        ("tts",),
        ("voice",),
        "读取本机模型包，不上传素材",
        label_en="Local Qwen3-TTS",
        description_en="Uses the model pack on this device. Media stays on this device.",
    ),
)


def get_preset(preset_id: str) -> AiProviderPreset | None:
    """Return a preset by stable ID."""

    return next((item for item in PROVIDER_PRESETS if item.preset_id == preset_id), None)


def presets_for_category(category: str) -> tuple[AiProviderPreset, ...]:
    """Return presets relevant to one Preferences category."""

    wanted = {
        "language": {"language"},
        "vision": {"vision", "image", "video"},
        "speech": {"asr", "tts"},
        "embedding": {"embedding"},
        "other": set(),
    }.get(category, set())
    if not wanted:
        return ()
    return tuple(
        item for item in PROVIDER_PRESETS
        if wanted.intersection(item.capabilities)
        or (category == "vision" and "vision_understanding" in item.capabilities)
    )


def infer_capabilities(provider_kind: str, modalities: Iterable[str]) -> tuple[str, ...]:
    """Infer capability tags for profiles written by older app versions."""

    values = {str(item) for item in modalities}
    kind = (provider_kind or "").lower()
    inferred: list[str] = []
    if values.intersection({"text", "tool_call"}) or kind in {
        "openai", "openai_responses", "volcengine_responses", "anthropic",
        "claude", "deepseek", "ollama", "local_http", "openai_compat",
    }:
        inferred.append("language")
    if values.intersection({"image", "video"}):
        inferred.append("vision")
    if "video" in values:
        inferred.append("video_understanding")
    if "audio" in values:
        inferred.append("audio_understanding")
    if values.intersection({"file", "media_upload"}):
        inferred.extend(("document_understanding", "file_input"))
    if "voice" in values or kind in {"volcengine_plan_tts", "local_qwen_tts"}:
        inferred.append("tts")
    if "asr" in values or kind == "local_qwen_asr":
        inferred.append("asr")
    if "embedding" in values or kind in {"embedding", "openai_embedding"}:
        inferred.append("embedding")
    return tuple(dict.fromkeys(inferred))


def _is_known_volcengine_multimodal(profile: object) -> bool:
    """识别旧配置中未迁移 capability 标签的 Doubao 全模态 Profile。

    早期用户配置可能只保存了 ``language`` / ``vision_understanding``，不能因为
    缺少新标签就把当前的 Seed 2.0 Lite 误判为纯文本模型。反过来不会把
    DeepSeek、GLM Coding 等其它 provider 误判成多模态。
    """
    kind = str(getattr(profile, "provider_kind", "") or "").lower()
    model = str(getattr(profile, "model_id", "") or "").lower().replace(".", "-")
    return kind == "volcengine_responses" and model.startswith("doubao-seed-2-0-")


def profile_supports_input(profile: object, input_kind: str) -> bool:
    """判断 Profile 是否声明/已知支持某类 Assistant 附件输入。

    ``input_kind`` 为 ``image`` / ``video`` / ``audio`` / ``file``。能力判断在
    controller 发送前完成，避免把不支持多模态的模型请求到一半才收到 400。
    """
    kind = str(input_kind or "file").lower()
    provider_kind = str(getattr(profile, "provider_kind", "") or "").lower()
    model = str(getattr(profile, "model_id", "") or "").lower()
    modalities = {str(item).lower() for item in (getattr(profile, "modalities", ()) or ())}
    capabilities = {str(item).lower() for item in (getattr(profile, "capabilities", ()) or ())}
    endpoint = str(getattr(profile, "endpoint", "") or "")
    try:
        official_deepseek = urlsplit(endpoint).hostname == "api.deepseek.com"
    except ValueError:
        official_deepseek = False
    if provider_kind == "deepseek" or official_deepseek:
        if kind != "image":
            return False
        if model in {"deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro"}:
            return False
        if model == "deepseek-flash" or model.startswith("deepseek-v4-flash"):
            return True
    if _is_known_volcengine_multimodal(profile):
        return True
    if "multimodal" in modalities or "multimodal_understanding" in capabilities:
        return True
    if kind == "image":
        return bool(
            {"image", "vision", "vision_understanding"}.intersection(modalities | capabilities)
        )
    # 当前项目的 video/audio/file transport 使用 Responses + File API；普通
    # Chat Completions client 没有文件上传实现，必须 fail closed。
    if provider_kind not in {"volcengine_responses", "openai_responses"}:
        return False
    if kind == "video":
        return bool(
            {"video", "video_understanding"}.intersection(modalities | capabilities)
        )
    if kind == "audio":
        return bool(
            {"audio", "audio_understanding", "asr"}.intersection(modalities | capabilities)
        )
    return bool(
        {"file", "file_input", "document_understanding", "media_upload"}.intersection(
            modalities | capabilities
        )
    )


def profile_multimodal_reason(profile: object) -> str:
    """返回面向用户的能力诊断，供 UI 在切换模型后给出明确提示。"""
    name = str(getattr(profile, "display_name", "当前模型") or "当前模型")
    model = str(getattr(profile, "model_id", "") or "未知模型")
    return f"{name}（{model}）不支持多模态文件输入，请切换至多模态模型。"


def category_for_profile(profile: object) -> str:
    """Map a profile to the tab that should display it."""

    capabilities = set(getattr(profile, "capabilities", ()) or ())
    if not capabilities:
        capabilities = set(infer_capabilities(
            str(getattr(profile, "provider_kind", "")),
            getattr(profile, "modalities", ()),
        ))
    if capabilities.intersection({"language"}):
        return "language"
    if capabilities.intersection({"vision", "image", "video"}):
        return "vision"
    if capabilities.intersection({"asr", "tts"}):
        return "speech"
    if "embedding" in capabilities:
        return "embedding"
    return "other"


__all__ = [
    "AiProviderPreset",
    "PROVIDER_PRESETS",
    "SOURCE_CUSTOM",
    "SOURCE_LOCAL",
    "SOURCE_OLLAMA",
    "SOURCE_OPTIONS",
    "SOURCE_PRESET",
    "category_for_profile",
    "get_preset",
    "infer_capabilities",
    "profile_multimodal_reason",
    "profile_supports_input",
    "presets_for_category",
]
