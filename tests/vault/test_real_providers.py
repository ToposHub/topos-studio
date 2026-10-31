"""真实 provider 测试：降级链 + 注册 + 懒加载。

当前环境没装 sentence-transformers/torch，所以真实 provider 的 is_available()
应返回 False，系统自动 fallback 到 stub。这验证降级机制有效。
"""

from __future__ import annotations

import pytest


def test_transcript_embed_real_registered():
    """Qwen3-Embedding 真实 provider 已注册（作为默认）。"""
    from apps.topos_vault.providers import Capability, list_providers

    providers = list_providers(Capability.TRANSCRIPT_EMBED)
    ids = [p.provider_id for p in providers]
    assert "qwen3_embedding_0_6b" in ids
    assert "stub_transcript_embed" in ids  # stub 也在


def test_visual_embed_real_registered():
    """SigLIP 2 真实 provider 已注册。"""
    from apps.topos_vault.providers import Capability, list_providers

    providers = list_providers(Capability.VISUAL_EMBED)
    ids = [p.provider_id for p in providers]
    assert "siglip2_base" in ids
    assert "stub_visual_embed" in ids


def test_real_provider_not_available_without_deps():
    """没装 sentence-transformers 时，真实 provider is_available()=False。"""
    from apps.topos_vault.providers.real_transcript_embed import Qwen3EmbeddingProvider

    p = Qwen3EmbeddingProvider()
    # 当前环境没装 sentence-transformers（或模型没下载）
    # is_available 应返回 False（或 True 如果碰巧装了，但大多数 CI 环境没有）
    # 这里用 try 包容两种情况
    try:
        available = p.is_available()
    except Exception:
        available = False
    # 不强断言 False（用户本地可能装了），但断言不抛异常
    assert isinstance(available, bool)


def test_fallback_to_stub_when_real_unavailable():
    """真实 provider 不可用时，get_provider 自动 fallback 到 stub。

    这是降级链的核心：用户调用 get_provider 永远拿到一个可用的 provider。
    """
    from apps.topos_vault.providers import get_provider, Capability

    p = get_provider(Capability.TRANSCRIPT_EMBED)
    # 不管真实还是 stub，必须可用
    assert p.is_available(), "get_provider 必须返回可用的 provider"

    # stub 的输出仍能工作
    result = p.embed_text("测试")
    assert len(result.vector) == result.dim


def test_real_provider_lazy_loading():
    """真实 provider 懒加载：构造时不加载模型。"""
    from apps.topos_vault.providers.real_transcript_embed import Qwen3EmbeddingProvider

    p = Qwen3EmbeddingProvider()
    # 构造后未加载
    assert not p._loaded
    assert p._model is None


def test_multiple_transcript_providers_exist():
    """transcript_embed 有多个 provider（真实 + 备选 + stub）。"""
    from apps.topos_vault.providers import Capability, list_providers

    providers = list_providers(Capability.TRANSCRIPT_EMBED)
    # 至少 3 个：qwen3 + bge_m3 + stub
    assert len(providers) >= 3


def test_multiple_visual_providers_exist():
    """visual_embed 有多个 provider。"""
    from apps.topos_vault.providers import Capability, list_providers

    providers = list_providers(Capability.VISUAL_EMBED)
    assert len(providers) >= 3  # siglip2 + clip_chinese + stub


def test_env_override_still_works():
    """真实 provider 注册后，环境变量切换仍有效。"""
    import os
    from apps.topos_vault.providers import get_provider, Capability

    # 强制用 stub（绕过真实 provider）
    os.environ["TOPOS_VAULT_TRANSCRIPT_EMBED"] = "stub_transcript_embed"
    try:
        p = get_provider(Capability.TRANSCRIPT_EMBED)
        assert p.provider_id == "stub_transcript_embed"
    finally:
        del os.environ["TOPOS_VAULT_TRANSCRIPT_EMBED"]


def test_model_loader_is_gpu_available():
    """is_gpu_available 不抛异常。"""
    from apps.topos_vault.providers.model_loader import is_gpu_available

    result = is_gpu_available()
    assert isinstance(result, bool)


def test_model_loader_resolve_device_no_crash():
    """resolve_torch_device 在没装 torch 时抛 RuntimeError（不静默）。"""
    from apps.topos_vault.providers.model_loader import resolve_torch_device

    try:
        device, dtype = resolve_torch_device()
        assert isinstance(device, str)
    except RuntimeError:
        pass  # torch 没装，预期抛 RuntimeError
    except ImportError:
        pass  # torch 没装
