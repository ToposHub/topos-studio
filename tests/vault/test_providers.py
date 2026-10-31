"""provider 层测试：Protocol + stub + 切换机制。"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------- #
# stub 注册
# ---------------------------------------------------------------------- #

def test_all_capabilities_have_stub():
    """6 个能力都有 stub 兜底（保证总有一个 provider 可用）。"""
    from apps.topos_vault.providers import Capability, list_providers

    for cap in Capability:
        providers = list_providers(cap)
        assert len(providers) >= 1, f"{cap.value} 没有 provider"
        stub_ids = [p.provider_id for p in providers if p.provider_id.startswith("stub_")]
        assert stub_ids, f"{cap.value} 没有 stub 兜底"


def test_stub_is_available():
    """stub 的 is_available() 永远 True。"""
    from apps.topos_vault.providers import Capability, get_provider

    for cap in Capability:
        # 默认 provider 应该是 stub（当前没注册 real 实现）
        p = get_provider(cap)
        assert p.is_available(), f"{cap.value} 默认 provider 不可用"


# ---------------------------------------------------------------------- #
# stub 输出确定性
# ---------------------------------------------------------------------- #

def test_stub_caption_deterministic():
    """stub caption 同输入同输出。"""
    from apps.topos_vault.providers import get_provider, Capability

    p = get_provider(Capability.VISUAL_CAPTION)
    r1 = p.caption_image("/test/a.jpg")
    r2 = p.caption_image("/test/a.jpg")
    assert r1.caption == r2.caption
    assert r1.tags == r2.tags


def test_stub_caption_different_inputs_differ():
    """不同输入 → 不同输出。"""
    from apps.topos_vault.providers import get_provider, Capability

    p = get_provider(Capability.VISUAL_CAPTION)
    r1 = p.caption_image("/test/a.jpg")
    r2 = p.caption_image("/test/b.jpg")
    assert r1.caption != r2.caption or r1.tags != r2.tags


def test_stub_embedding_correct_dimension():
    """stub 向量维度正确。"""
    from apps.topos_vault.providers import get_provider, Capability

    # transcript_embed: 1024 维
    p = get_provider(Capability.TRANSCRIPT_EMBED)
    r = p.embed_text("你好世界")
    assert r.dim == 1024
    assert len(r.vector) == 1024

    # visual_embed: 768 维（SigLIP2 base-patch16-224 的 hidden_size）
    p = get_provider(Capability.VISUAL_EMBED)
    r = p.embed_image("/test.jpg")
    assert r.dim == 768
    assert len(r.vector) == 768


def test_stub_embedding_deterministic():
    """stub 向量确定性。"""
    from apps.topos_vault.providers import get_provider, Capability

    p = get_provider(Capability.TRANSCRIPT_EMBED)
    r1 = p.embed_text("微笑")
    r2 = p.embed_text("微笑")
    assert r1.vector == r2.vector


def test_stub_batch_embedding():
    """批量 embed。"""
    from apps.topos_vault.providers import get_provider, Capability

    p = get_provider(Capability.TRANSCRIPT_EMBED)
    results = p.embed_texts(["你好", "世界", "微笑"])
    assert len(results) == 3
    assert all(r.dim == 1024 for r in results)


# ---------------------------------------------------------------------- #
# 切换机制
# ---------------------------------------------------------------------- #

def test_env_var_override(monkeypatch):
    """环境变量能切换 provider。"""
    from apps.topos_vault.providers import (
        Capability, ProviderConfig, ProviderRegistry,
    )
    from apps.topos_vault.providers.stubs import register_stubs, StubVisualCaptionProvider

    # 用独立 registry 测试（不污染全局）
    reg = ProviderRegistry()
    register_stubs(reg)

    # 注册第二个 stub 变体
    class AltCaption(StubVisualCaptionProvider):
        provider_id = "alt_visual_caption"
    from apps.topos_vault.providers.base import ProviderDescriptor
    reg.register(AltCaption(), ProviderDescriptor(
        provider_id="alt_visual_caption",
        capability=Capability.VISUAL_CAPTION,
        display_name="替代", requires_gpu=False, min_vram_mb=0,
    ))

    # 默认走 stub_visual_caption
    config = ProviderConfig()
    p1 = config.select(Capability.VISUAL_CAPTION, reg)
    assert p1.provider_id == "stub_visual_caption"

    # 环境变量切到 alt
    monkeypatch.setenv("TOPOS_VAULT_VISUAL_CAPTION", "alt_visual_caption")
    p2 = config.select(Capability.VISUAL_CAPTION, reg)
    assert p2.provider_id == "alt_visual_caption"


def test_config_override():
    """ProviderConfig.overrides 能显式指定。"""
    from apps.topos_vault.providers import (
        Capability, ProviderConfig, ProviderRegistry,
    )
    from apps.topos_vault.providers.base import ProviderDescriptor
    from apps.topos_vault.providers.stubs import register_stubs, StubTranscriptEmbedProvider

    reg = ProviderRegistry()
    register_stubs(reg)

    class CustomEmbed(StubTranscriptEmbedProvider):
        provider_id = "custom_embed"
    reg.register(CustomEmbed(), ProviderDescriptor(
        provider_id="custom_embed",
        capability=Capability.TRANSCRIPT_EMBED,
        display_name="自定义", requires_gpu=False, min_vram_mb=0,
    ))

    config = ProviderConfig(overrides={Capability.TRANSCRIPT_EMBED: "custom_embed"})
    p = config.select(Capability.TRANSCRIPT_EMBED, reg)
    assert p.provider_id == "custom_embed"


def test_invalid_env_var_raises(monkeypatch):
    """环境变量指定不存在的 provider → 报错。"""
    from apps.topos_vault.providers import Capability, ProviderConfig

    monkeypatch.setenv("TOPOS_VAULT_VISUAL_CAPTION", "nonexistent_provider")
    with pytest.raises(ValueError, match="未注册"):
        ProviderConfig().select(Capability.VISUAL_CAPTION)


def test_registry_rejects_duplicate():
    """重复注册同 provider_id → 报错。"""
    from apps.topos_vault.providers import Capability, ProviderRegistry
    from apps.topos_vault.providers.base import ProviderDescriptor
    from apps.topos_vault.providers.stubs import StubAsrProvider

    reg = ProviderRegistry()
    desc = ProviderDescriptor(
        provider_id="dup", capability=Capability.ASR,
        display_name="x", requires_gpu=False, min_vram_mb=0,
    )
    reg.register(StubAsrProvider(), desc, is_default=True)
    # 注意：StubAsrProvider 的 provider_id 是 "stub_asr"，这里用同实例再注册同 desc 会因为
    # provider_id "dup" 已存在而报错——但 StubAsrProvider.provider_id 是 "stub_asr"，
    # 与 desc.provider_id="dup" 不一致，会先触发 capability/ provider_id 不一致检查。
    # 改用一个 provider_id 匹配的 stub：
    class DupStub(StubAsrProvider):
        provider_id = "dup"
    with pytest.raises(ValueError):
        reg.register(DupStub(), desc)


def test_list_providers_by_capability():
    """按能力列 provider。"""
    from apps.topos_vault.providers import Capability, list_providers

    visual = list_providers(Capability.VISUAL_CAPTION)
    assert all(p.capability == Capability.VISUAL_CAPTION for p in visual)
    assert len(visual) >= 1


def test_list_all_providers():
    """列全部 provider。"""
    from apps.topos_vault.providers import list_providers

    all_p = list_providers()
    assert len(all_p) >= 6  # 至少 6 个 stub
