"""AI 能力的可插拔 provider 层。

每个能力定义一个 Protocol（契约），有多个实现（大/小/stub）可切换。
完全对齐项目主 app 既有的 AnalysisProvider + _real_inference_enabled + ModelPackSpec
模式——"换更小模型" = 加一个新实现 + 改配置项，流水线代码不动。

6 个能力：

    transcript_embed   台词/文本 → 向量（用于语义检索）
    visual_embed       画面 → 向量（视频镜头和图片共用）
    visual_caption     画面 → caption/tags（视频镜头和图片共用）
    audio_understand   音乐 → 情绪/乐器/描述 + 数值特征(BPM)
    face_recognize     画面 → 人脸向量/属性
    asr                视频 → 台词（复用主项目，vault 不重做）

切换机制（三种，由易到难）：
    1. 环境变量 TOPOS_VAULT_<CAP>=<provider_id>
    2. 配置文件 [providers] <cap> = <provider_id>
    3. 自动 fallback（路由器模式，质量不达标降级）

详见 apps/topos_vault/providers/base.py。
"""

from apps.topos_vault.providers.base import (
    Capability,
    ProviderConfig,
    ProviderRegistry,
    get_provider,
    list_providers,
)
from apps.topos_vault.providers import stubs  # noqa: F401 — 副作用导入：注册 stub 实现（先注册，真实 provider 覆盖默认）

# 注册真实 provider（is_available()=False 时自动降级到 stub）
# 用 try 避免 import 错误阻塞（如 sentence-transformers 未装）
try:
    from apps.topos_vault.providers.real_registry import register_all_real_providers
    register_all_real_providers()
except Exception:
    pass  # 真实 provider 注册失败不阻塞——stub 已就绪

__all__ = [
    "Capability",
    "ProviderConfig",
    "ProviderRegistry",
    "get_provider",
    "list_providers",
]
