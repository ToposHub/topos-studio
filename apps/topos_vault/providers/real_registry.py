"""真实 provider 的统一注册入口。

应用启动时调用 ``register_all_real_providers()``，把所有真实 provider
注册到 registry。真实 provider 注册后成为默认（is_default=True），
但只有 is_available()=True 时才会被选中——否则 fallback 到 stub。

降级链：
    真实 provider（模型已装）→ 备选真实 provider → stub 兜底
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register_all_real_providers() -> None:
    """注册所有真实 provider（应用启动时调用）。

    幂等：重复调用不重复注册。
    每个真实 provider 注册时 is_default=True 覆盖 stub，
    但 ProviderConfig.select 的 fallback 逻辑保证：
    如果真实 provider is_available()=False，会自动降到 stub。
    """
    from apps.topos_vault.providers.real_transcript_embed import register_real_transcript_embed
    from apps.topos_vault.providers.real_visual_embed import register_real_visual_embed
    from apps.topos_vault.providers.real_visual_caption import register_real_visual_caption
    from apps.topos_vault.providers.real_audio_understand import register_real_audio_understand
    from apps.topos_vault.providers.real_face_recognize import register_real_face_recognize

    for register_fn, name in [
        (register_real_transcript_embed, "transcript_embed"),
        (register_real_visual_embed, "visual_embed"),
        (register_real_visual_caption, "visual_caption"),
        (register_real_audio_understand, "audio_understand"),
        (register_real_face_recognize, "face_recognize"),
    ]:
        try:
            register_fn()
        except Exception as e:
            logger.warning("注册 %s 真实 provider 失败：%s", name, e)

    logger.info("真实 provider 注册完成（可用的用真实模型，不可用的用 stub 兜底）")
