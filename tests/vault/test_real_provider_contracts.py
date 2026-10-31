"""真实 provider 代码契约测试（P0-1 联调修复的回归保护）。

这些测试不下载模型、不跑真实推理，只验证"联调会踩的坑"在代码层已修复：
    1. SigLIP2 维度 = 768（不是 1152）
    2. Qwen3-Embedding 维度 = 1024
    3. 向量输出统一成 list[float] + 正确 dim（numpy/torch 兼容）
    4. caption / audio 输出解析正确（模型输出格式约定）
    5. _resolve_local_or_hf：模型未装时返回 HF id（不崩），装了返回本地路径
    6. is_available 在没模型时返回 False（触发 stub fallback）

没装 sentence-transformers/torch 的 CI 环境也能跑（import 失败时优雅跳过）。
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------- #
# 维度常量（联调最常踩的坑：模型维度写错 → 向量库维度不匹配）
# ---------------------------------------------------------------------- #

def test_siglip2_dimension_is_768():
    """SigLIP2 base-patch16-224 的 hidden_size = 768（不是 1152）。

    1152 是 giant/so400m 的维度。base 写 1152 会导致向量库建库维度与真实输出不符。
    """
    from apps.topos_vault.providers.real_visual_embed import SigLIP2EmbeddingProvider
    assert SigLIP2EmbeddingProvider.DIM == 768


def test_qwen3_embedding_dimension_is_1024():
    """Qwen3-Embedding-0.6B 输出 1024 维。"""
    from apps.topos_vault.providers.real_transcript_embed import Qwen3EmbeddingProvider
    assert Qwen3EmbeddingProvider.DIM == 1024


def test_insightface_dimension_is_512():
    """InsightFace buffalo_l 的 normed_embedding = 512 维。"""
    from apps.topos_vault.providers.real_face_recognize import InsightFaceProvider
    assert InsightFaceProvider.DIM == 512


def test_vector_store_default_dims_match_providers():
    """VectorStoreFactory.DEFAULT_DIMS 与 provider 实际维度对齐。"""
    from apps.topos_vault.db.faiss_store import VectorStoreFactory
    assert VectorStoreFactory.DEFAULT_DIMS["visual"] == 768
    assert VectorStoreFactory.DEFAULT_DIMS["transcript"] == 1024
    assert VectorStoreFactory.DEFAULT_DIMS["face"] == 512


def test_stub_visual_embed_matches_real_dim():
    """stub visual_embed 维度与真实 SigLIP2 一致（让 stub/真实可互换）。"""
    from apps.topos_vault.providers.stubs import StubVisualEmbedProvider
    from apps.topos_vault.providers.real_visual_embed import SigLIP2EmbeddingProvider
    stub = StubVisualEmbedProvider()
    r = stub.embed_image("/x.jpg")
    assert r.dim == SigLIP2EmbeddingProvider.DIM == 768


# ---------------------------------------------------------------------- #
# 向量输出统一化（numpy/torch → list[float]）
# ---------------------------------------------------------------------- #

def test_vec_to_list_helpers_handle_numpy_and_list():
    """_to_float_list / _vec_to_list 兼容 numpy、list、torch（有 tolist）。"""
    import numpy as np
    from apps.topos_vault.providers.real_transcript_embed import _to_float_list
    from apps.topos_vault.providers.real_visual_embed import _vec_to_list

    # numpy
    assert _to_float_list(np.array([1.0, 2.0])) == [1.0, 2.0]
    assert _vec_to_list(np.array([1.0, 2.0])) == [1.0, 2.0]
    # list
    assert _to_float_list([1, 2, 3]) == [1.0, 2.0, 3.0]
    # 模拟 torch tensor（有 tolist）
    class FakeTensor:
        def tolist(self):
            return [0.1, 0.2]
    assert _to_float_list(FakeTensor()) == [0.1, 0.2]


def test_vec_dim_helpers():
    """_vec_dim 兼容有 shape 和无 shape 的向量。"""
    import numpy as np
    from apps.topos_vault.providers.real_transcript_embed import _vec_dim
    from apps.topos_vault.providers.real_visual_embed import _vec_dim as _vd2

    assert _vec_dim(np.array([1.0] * 768)) == 768
    assert _vd2(np.array([1.0] * 768)) == 768
    assert _vec_dim([1, 2, 3, 4]) == 4


# ---------------------------------------------------------------------- #
# 模型输出解析（caption / audio 格式约定）
# ---------------------------------------------------------------------- #

def test_parse_caption_output_structured():
    """Qwen3-VL 输出按 prompt 约定的格式（描述：/标签：）能正确解析。"""
    from apps.topos_vault.providers.real_visual_caption import _parse_caption_output
    cap, tags = _parse_caption_output("描述：一只猫在桌上\n标签：猫, 桌子, 室内")
    assert cap == "一只猫在桌上"
    assert tags == ["猫", "桌子", "室内"]


def test_parse_caption_output_chinese_comma():
    """标签用中文逗号也能解析。"""
    from apps.topos_vault.providers.real_visual_caption import _parse_caption_output
    cap, tags = _parse_caption_output("描述：夜景\n标签：城市，灯光，建筑")
    assert cap == "夜景"
    assert tags == ["城市", "灯光", "建筑"]


def test_parse_caption_output_colon_variant():
    """半角冒号也能解析。"""
    from apps.topos_vault.providers.real_visual_caption import _parse_caption_output
    cap, tags = _parse_caption_output("描述:夜景\n标签:城市,灯光")
    assert cap == "夜景"
    assert tags == ["城市", "灯光"]


def test_parse_caption_output_fallback_to_whole_text():
    """模型没按格式输出时，整段文本做 caption（不丢内容）。"""
    from apps.topos_vault.providers.real_visual_caption import _parse_caption_output
    cap, tags = _parse_caption_output("这是一段自由格式的描述")
    assert cap == "这是一段自由格式的描述"
    assert tags == []


def test_parse_audio_output_full():
    """Qwen2.5-Omni 输出按 prompt 约定的格式能解析。"""
    from apps.topos_vault.providers.real_audio_understand import _parse_audio_output
    mood, instr, vt, desc = _parse_audio_output(
        "情绪：energetic\n乐器：鼓, 吉他\n类型：instrumental\n描述：欢快的鼓点"
    )
    assert mood == "energetic"
    assert instr == ["鼓", "吉他"]
    assert vt == "instrumental"
    assert desc == "欢快的鼓点"


def test_parse_audio_output_mood_lowercased():
    """情绪标签统一小写（方便检索过滤）。"""
    from apps.topos_vault.providers.real_audio_understand import _parse_audio_output
    mood, _, _, _ = _parse_audio_output("情绪：HAPPY")
    assert mood == "happy"


# ---------------------------------------------------------------------- #
# 本地模型路径解析（_resolve_local_or_hf）
# ---------------------------------------------------------------------- #

def test_resolve_returns_hf_id_when_model_not_installed(vault_dir):
    """模型未装时，_resolve_local_or_hf 返回 HF id（供 sentence-transformers 联网下载）。"""
    from apps.topos_vault.providers.real_transcript_embed import Qwen3EmbeddingProvider
    p = Qwen3EmbeddingProvider()
    hf_id = "Qwen/Qwen3-Embedding-0.6B"
    assert p._resolve_local_or_hf(hf_id) == hf_id


def test_resolve_returns_local_path_when_installed(vault_dir, tmp_path, monkeypatch):
    """模型已下载到 models/ai_editing/<pack_id>/ 时，返回本地路径（不联网）。"""
    from apps.topos_vault.providers.real_visual_embed import SigLIP2EmbeddingProvider
    # 造一个本地模型目录
    fake_models = tmp_path / "models" / "ai_editing"
    pack = fake_models / "siglip2_base"
    pack.mkdir(parents=True)
    (pack / "config.json").write_text("{}")
    monkeypatch.setenv("TOPOS_VAULT_MODELS_DIR", str(fake_models))

    p = SigLIP2EmbeddingProvider()
    resolved = p._resolve_local_or_hf("sentence-transformers/siglip2-base-patch16-224")
    assert str(pack) == resolved  # 返回本地路径，不是 HF id


def test_resolve_no_pack_id_returns_hf():
    """没有 model_pack_id 时，直接返回 HF id。"""
    from apps.topos_vault.providers.model_loader import LazyModelLoader
    loader = LazyModelLoader(provider_id="test", model_pack_id=None)
    assert loader._resolve_local_or_hf("some/model") == "some/model"


# ---------------------------------------------------------------------- #
# is_available 在无模型时返回 False（触发 stub fallback）
# ---------------------------------------------------------------------- #

def test_real_providers_not_available_without_model(vault_dir, monkeypatch):
    """没下载模型时，所有真实 provider 的 is_available() 应返回 False。

    这保证 ProviderConfig.select 自动 fallback 到 stub，不会在加载时崩溃。
    覆盖 P0-1 修复的核心：_check_dependencies 要查模型文件，不只查依赖库。
    """
    # 清掉 HF 缓存检查（CI 环境可能有其他模型缓存，但不该误判 vault 模型）
    monkeypatch.setenv("TOPOS_VAULT_MODELS_DIR", str(vault_dir / "empty_models"))

    from apps.topos_vault.providers.real_transcript_embed import Qwen3EmbeddingProvider
    from apps.topos_vault.providers.real_visual_embed import SigLIP2EmbeddingProvider

    # Qwen3-Embedding：装了 sentence-transformers 但没模型 → False
    p1 = Qwen3EmbeddingProvider()
    try:
        assert p1.is_available() is False
    except Exception:
        # 依赖没装的环境也是 False
        assert p1.is_available() is False

    # SigLIP2：同上
    p2 = SigLIP2EmbeddingProvider()
    assert p2.is_available() is False


# ---------------------------------------------------------------------- #
# 端到端：stub 在真实 provider 不可用时正常工作（降级链）
# ---------------------------------------------------------------------- #

def test_pipeline_works_with_stub_when_real_unavailable(vault_dir):
    """真实 provider 不可用时，pipeline 用 stub 完成打标（不崩）。

    这是 P0-1 的最终目标：不管模型装没装，index 命令都能跑完。
    """
    import os
    from pathlib import Path
    from apps.topos_vault.db import schema
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    schema.init_db()
    repo = VaultRepository()

    # 造一个图片文件
    img = Path(vault_dir) / "test.jpg"
    img.write_bytes(b"FAKE_IMAGE" * 100)

    orch = Orchestrator(repo)
    results = orch.index_file(str(img))

    # 至少 ingest 成功
    from apps.topos_vault.pipeline.steps import StepStatus
    ingest = next(r for r in results if r.step_name == "ingest")
    assert ingest.status == StepStatus.DONE

    # visual_embed 用 stub（768 维）应能写入
    visual = next((r for r in results if r.step_name == "visual_embed"), None)
    assert visual is not None
    # stub 可用 → done
    assert visual.status == StepStatus.DONE
