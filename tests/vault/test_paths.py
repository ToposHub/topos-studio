"""paths 模块测试：跨平台路径解析。"""

from __future__ import annotations

import os
from pathlib import Path


def test_vault_dir_respects_env(vault_dir):
    """TOPOS_VAULT_DIR 环境变量能覆盖默认路径。"""
    from apps.topos_vault.paths import get_vault_dir
    assert get_vault_dir() == vault_dir


def test_vault_dir_auto_created(tmp_path, monkeypatch):
    """目录不存在时自动创建。"""
    target = tmp_path / "nested" / "vault"
    monkeypatch.setenv("TOPOS_VAULT_DIR", str(target))
    from apps.topos_vault.paths import get_vault_dir
    assert get_vault_dir().exists()


def test_db_path_under_vault(vault_dir):
    """vault.db 路径在 vault 目录下。"""
    from apps.topos_vault.paths import get_db_path
    assert get_db_path() == vault_dir / "vault.db"


def test_faiss_dir_created(vault_dir):
    """faiss 目录自动创建。"""
    from apps.topos_vault.paths import get_faiss_dir
    faiss = get_faiss_dir()
    assert faiss.exists()
    assert faiss.name == "faiss"


def test_cache_dir_bucketing(vault_dir):
    """缓存按指纹前 2 位分桶：cache/a1/b2/<fp>/。"""
    from apps.topos_vault.paths import get_cache_dir

    fp = "xxh3_3pt:a1b2c3d4e5f6"
    cache = get_cache_dir(fp)
    assert cache.exists()
    assert cache.parent.parent.parent.name == "cache"
    # 路径里含指纹的前几位做分桶
    assert "a1" in str(cache)
    assert "b2" in str(cache)


def test_cache_dir_no_fingerprint(vault_dir):
    """不传指纹时返回 cache 根目录。"""
    from apps.topos_vault.paths import get_cache_dir
    root = get_cache_dir(None)
    assert root.exists()
    assert root.name == "cache"


def test_models_dir_defaults_to_project(vault_dir):
    """模型目录默认指向项目内 models/ai_editing/。"""
    from apps.topos_vault.paths import get_models_dir
    models = get_models_dir()
    assert "models" in str(models)
    assert "ai_editing" in str(models)


def test_models_dir_subdir(vault_dir):
    """子目录拼接正确。"""
    from apps.topos_vault.paths import get_models_dir
    models = get_models_dir("qwen3_asr")
    assert models.name == "qwen3_asr"


def test_models_dir_env_override(vault_dir, tmp_path, monkeypatch):
    """TOPOS_VAULT_MODELS_DIR 能覆盖模型目录。"""
    custom = tmp_path / "custom_models"
    monkeypatch.setenv("TOPOS_VAULT_MODELS_DIR", str(custom))
    from apps.topos_vault.paths import get_models_dir
    assert get_models_dir() == custom.resolve()


def test_describe_paths(vault_dir):
    """describe_paths 返回完整路径信息。"""
    from apps.topos_vault.paths import describe_paths
    info = describe_paths()
    assert "vault_dir" in info
    assert "db_path" in info
    assert "models_dir" in info
    assert str(vault_dir) in info["vault_dir"]
