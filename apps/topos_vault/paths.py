"""跨平台路径解析。

vault 是独立外挂应用，数据必须放**用户目录**（跨工程共享），不能用项目内的
``.topos/``（那是主项目源码模式的隔离配置）。用 ``platformdirs`` 处理三平台差异：

    macOS:  ~/Library/Application Support/topos-vault
    Windows: C:\\Users\\<u>\\AppData\\Local\\topos-vault
    Linux:  ~/.local/share/topos-vault

模型权重**不放这里**——放项目内 ``models/ai_editing/``，与主项目共享，复用
``src.shared.paths.get_models_dir`` 与 ``ModelPackManager``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# platformdirs 是可选依赖：未安装时回退到 ~/.topos-vault（仍跨平台可用，
# 只是 Windows 上不遵循 %APPDATA% 约定）。安装方式：pip install platformdirs
try:
    from platformdirs import user_data_dir

    _HAS_PLATFORMDIRS = True
except ImportError:
    _HAS_PLATFORMDIRS = False


_APP_NAME = "topos-vault"


def get_vault_dir() -> Path:
    """vault 数据根目录（跨平台，用户级）。

    环境变量 ``TOPOS_VAULT_DIR`` 可强制覆盖（用于测试或多库切换）。
    目录不存在时自动创建。
    """
    override = os.environ.get("TOPOS_VAULT_DIR")
    if override:
        vault_dir = Path(override).expanduser().resolve()
    elif _HAS_PLATFORMDIRS:
        # appauthor=False：不加 "Topos" 公司前缀，路径更短
        vault_dir = Path(user_data_dir(_APP_NAME, appauthor=False))
    else:
        # 回退：所有平台都用 ~/.topos-vault（跨平台可用，但不遵循 Win 约定）
        vault_dir = Path.home() / ".topos-vault"

    vault_dir.mkdir(parents=True, exist_ok=True)
    return vault_dir


def get_db_path() -> Path:
    """SQLite 主库路径：``<vault>/vault.db``。"""
    return get_vault_dir() / "vault.db"


def get_faiss_dir() -> Path:
    """FAISS 向量索引目录：``<vault>/faiss/``。

    四个独立索引文件（绝不合并）：
        transcript.faiss      台词向量（Qwen3-Embedding, 1024维）
        face.faiss            人脸向量（InsightFace, 512维）
        visual.faiss          画面向量（SigLIP 2, 视频和图片共用）
        audio_semantic.faiss  音乐语义向量（Qwen2.5-Omni）
    """
    faiss_dir = get_vault_dir() / "faiss"
    faiss_dir.mkdir(parents=True, exist_ok=True)
    return faiss_dir


def get_cache_dir(fingerprint: str | None = None) -> Path:
    """派生产物缓存目录。

    按指纹前 2 位分桶（避免单目录文件数爆炸）：
        <vault>/cache/a1/b2/<fingerprint>/

    Args:
        fingerprint: 素材指纹。传入则返回该素材专属缓存目录；None 返回 cache 根。
    """
    cache_root = get_vault_dir() / "cache"
    if fingerprint is None:
        cache_root.mkdir(parents=True, exist_ok=True)
        return cache_root

    # 取指纹前 2 字符做两级分桶：a1/b2/<fp>/
    # 指纹格式如 "xxh3_3pt:a1b2c3..." 或 "phash:abcd1234..."
    fp_clean = fingerprint.split(":", 1)[-1].replace("-", "")
    bucket1 = fp_clean[:2] if len(fp_clean) >= 2 else "_"
    bucket2 = fp_clean[2:4] if len(fp_clean) >= 4 else "_"

    target = cache_root / bucket1 / bucket2 / fingerprint
    target.mkdir(parents=True, exist_ok=True)
    return target


def get_chromaprint_index_path() -> Path:
    """音乐声纹索引路径（Chromaprint，用于音乐素材重连）。"""
    return get_vault_dir() / "chromaprint.index"


def get_project_root() -> Path:
    """Topos Color 项目根目录（用于定位共享的 models/ai_editing/）。

    从本文件向上推导：apps/topos_vault/paths.py → parents[3] = 项目根。
    与 ``src.shared.paths.get_project_root`` 对齐，但本应用可能脱离主项目独立
    运行（用户只装了 topos-vault），所以走环境变量优先。
    """
    override = os.environ.get("TOPOS_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    _here = Path(__file__).resolve()
    return _here.parents[3]


def get_models_dir(subdir: str = "") -> Path:
    """模型权重目录（与主项目共享）。

    优先级：
    1. ``TOPOS_VAULT_MODELS_DIR`` 环境变量（vault 独立部署时用）
    2. 项目内 ``models/ai_editing/``（与主项目共享，复用 ModelPackManager）

    vault 新增的模型包都落在 ``models/ai_editing/`` 下（与 qwen3_asr 等同源），
    这样主项目已下载的模型 vault 直接复用，反之亦然。
    """
    override = os.environ.get("TOPOS_VAULT_MODELS_DIR")
    if override:
        models = Path(override).expanduser().resolve()
    else:
        # 对齐 src/shared/paths/paths.py：项目根/models/ai_editing/
        models = get_project_root() / "models" / "ai_editing"

    if subdir:
        models = models / subdir

    # models/ 可能在 .gitignore 里不存在（首次运行），不强制创建
    return models


def is_platformdirs_available() -> bool:
    """platformdirs 是否已安装（用于诊断信息）。"""
    return _HAS_PLATFORMDIRS


def describe_paths() -> dict[str, str]:
    """返回所有关键路径的描述（用于 CLI status 命令和诊断）。"""
    return {
        "vault_dir": str(get_vault_dir()),
        "db_path": str(get_db_path()),
        "faiss_dir": str(get_faiss_dir()),
        "cache_dir": str(get_cache_dir()),
        "models_dir": str(get_models_dir()),
        "project_root": str(get_project_root()),
        "platformdirs": "installed" if _HAS_PLATFORMDIRS else "missing (fallback to ~/.topos-vault)",
        "python": sys.executable,
    }
