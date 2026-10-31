"""vault 测试的公共 fixtures。

核心 fixture：``vault_dir`` —— 每个测试用独立的临时目录，
通过 TOPOS_VAULT_DIR 隔离，绝不碰用户真实数据。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def vault_dir(tmp_path, monkeypatch):
    """隔离的 vault 数据目录（每个测试独立，自动清理）。

    通过 monkeypatch 设置 TOPOS_VAULT_DIR，让 paths.get_vault_dir() 返回临时目录。
    测试结束后 tmp_path 自动清理。
    """
    d = tmp_path / "vault"
    d.mkdir()
    monkeypatch.setenv("TOPOS_VAULT_DIR", str(d))
    return d


@pytest.fixture
def initialized_db(vault_dir):
    """已初始化的 vault.db（建表 + 迁移完成）。"""
    from apps.topos_vault.db import schema
    schema.init_db()
    return vault_dir / "vault.db"


@pytest.fixture
def repo(initialized_db):
    """已初始化的 VaultRepository。"""
    from apps.topos_vault.db.repository import VaultRepository
    return VaultRepository()


@pytest.fixture
def sample_files(tmp_path):
    """创建一组测试用的素材文件（不同类型、不同大小）。

    返回 dict：{'video': path, 'audio': path, 'image': path, 'image2': path}
    """
    media = tmp_path / "media"
    media.mkdir()

    files = {}
    # 视频（用不同内容避免 size 降级误配）
    files["video"] = media / "clip.mp4"
    files["video"].write_bytes(b"VIDEO_CONTENT_AAA" * 1000)
    # 音频
    files["audio"] = media / "track.mp3"
    files["audio"].write_bytes(b"AUDIO_CONTENT_BBB" * 800)
    # 图片
    files["image"] = media / "photo.jpg"
    files["image"].write_bytes(b"IMAGE_CONTENT_CCC" * 500)
    files["image2"] = media / "photo2.jpg"
    files["image2"].write_bytes(b"IMAGE_CONTENT_DDD" * 400)

    return files
