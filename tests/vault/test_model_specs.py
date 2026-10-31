"""model_specs 测试：模型包描述 + 安装检查。"""

from __future__ import annotations


def test_all_capabilities_have_spec():
    """5 个 vault 能力都有对应的模型包 spec（ASR 复用主项目，不在 vault specs 里）。"""
    from apps.topos_vault.model_specs import VAULT_MODEL_SPECS

    capabilities = {s.capability for s in VAULT_MODEL_SPECS}
    expected = {"transcript_embed", "visual_embed", "visual_caption",
                "audio_understand", "face_recognize"}
    assert expected.issubset(capabilities), f"缺少：{expected - capabilities}"


def test_get_spec_by_pack_id():
    """按 pack_id 查。"""
    from apps.topos_vault.model_specs import get_spec

    s = get_spec("qwen3_embedding_0_6b")
    assert s is not None
    assert s.capability == "transcript_embed"

    assert get_spec("nonexistent") is None


def test_specs_by_capability():
    """按能力列 spec。"""
    from apps.topos_vault.model_specs import specs_by_capability

    embed_specs = specs_by_capability("transcript_embed")
    assert len(embed_specs) >= 1
    assert all(s.capability == "transcript_embed" for s in embed_specs)


def test_install_dir_under_models(vault_dir):
    """安装目录在 models/ai_editing/ 下。"""
    from apps.topos_vault.model_specs import get_spec

    s = get_spec("siglip2_base")
    assert "models" in str(s.install_dir)
    assert "ai_editing" in str(s.install_dir)
    assert s.install_dir.name == "siglip2_base"


def test_is_installed_false_when_no_dir(vault_dir):
    """pack 目录不存在 → is_installed=False。"""
    from apps.topos_vault.model_specs import get_spec

    s = get_spec("qwen3_vl_8b_awq")
    assert not s.is_installed


def test_is_installed_true_when_dir_has_files(vault_dir, tmp_path, monkeypatch):
    """pack 目录有文件 → is_installed=True。"""
    from apps.topos_vault.model_specs import get_spec

    # 指向一个有文件的目录
    fake_models = tmp_path / "models" / "ai_editing"
    fake_pack = fake_models / "siglip2_base"
    fake_pack.mkdir(parents=True)
    (fake_pack / "model.safetensors").write_bytes(b"fake")
    monkeypatch.setenv("TOPOS_VAULT_MODELS_DIR", str(fake_models))

    s = get_spec("siglip2_base")
    assert s.is_installed


def test_check_installation_status():
    """安装状态检查返回所有 pack。"""
    from apps.topos_vault.model_specs import check_installation_status

    statuses = check_installation_status()
    assert len(statuses) >= 5
    for s in statuses:
        assert "pack_id" in s
        assert "installed" in s
        assert "capability" in s
        assert isinstance(s["installed"], bool)


def test_to_dict_matches_main_project_shape():
    """to_dict 的结构与主项目 ModelPackSpec.to_dict 对齐。"""
    from apps.topos_vault.model_specs import get_spec

    s = get_spec("insightface_buffalo_l")
    d = s.to_dict()
    # 主项目 ModelPackSpec.to_dict 的字段
    expected_keys = {"pack_id", "capability", "version", "size_bytes", "sha256",
                     "languages", "requires_gpu", "min_vram_mb", "license",
                     "download_url", "notes"}
    assert expected_keys.issubset(d.keys())
    assert "name" in d["license"]
    assert "commercial_use" in d["license"]


def test_all_licenses_commercial_safe():
    """所有 vault 模型都是商用友好（Apache/MIT/Qwen Apache）。

    这是 vault 模型选型的硬约束——影视后期商用场景不能用 CC-BY-NC。
    """
    from apps.topos_vault.model_specs import VAULT_MODEL_SPECS

    for s in VAULT_MODEL_SPECS:
        assert s.license.commercial_use, (
            f"{s.pack_id} 许可证不允许商用：{s.license.name}"
        )
