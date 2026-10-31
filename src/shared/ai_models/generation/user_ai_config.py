"""用户级 AI 配置文件（``~/.topos/ai_profiles.json``）。

配置与凭据集中放在用户 home 目录，便于用户备份和手动编辑。文件由原子写入
更新，并尽量使用仅用户可读的权限（目录 ``0700``、文件 ``0600``）。API key
仍然是明文 JSON，因此不应把该文件提交到版本库或放在共享账户中。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

USER_AI_CONFIG_DIR = Path.home() / ".topos"
USER_AI_CONFIG_PATH = USER_AI_CONFIG_DIR / "ai_profiles.json"


class UserAiConfigError(RuntimeError):
    """用户级配置读取失败（暂时性 IO/解析错误）。

    与"文件不存在"（合法的空配置）区分（AUD-31623）：调用方——尤其是
    read-modify-write 整体覆盖型调用方——不得在读取失败时以仅含增量的
    空配置落盘，否则原有 profiles 与凭据会全部丢失。
    """


def load_user_ai_config(path: Path | None = None, *, strict: bool = False) -> dict[str, Any]:
    """读取用户级配置。

    - 文件不存在 → 空配置（合法首启状态，``strict`` 下同样返回空）。
    - 文件存在但读取/解析失败（权限、占用、损坏）：
      - ``strict=False``（默认，只读容错）：记 warning 并返回空配置；
      - ``strict=True``（覆盖前预检）：抛 :class:`UserAiConfigError`。
    """

    target = path or USER_AI_CONFIG_PATH
    try:
        with target.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        if strict:
            raise UserAiConfigError(f"failed to load {target}: {exc!r}") from exc
        logger.warning("user AI config %s unreadable, degrading to empty: %r", target, exc)
        return {}
    return dict(value) if isinstance(value, dict) else {}


def save_user_ai_config(
    data: Mapping[str, Any], path: Path | None = None
) -> None:
    """原子保存用户级配置，并设置收紧的文件权限。

    覆盖守卫（AUD-31623）：目标文件已存在但不可读/解析失败时抛
    :class:`UserAiConfigError` 拒绝覆盖；可读时先保留一代 ``.bak`` 备份。
    """

    target = path or USER_AI_CONFIG_PATH
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # 覆盖前 strict 预读：暂时性读取失败不得被整体覆盖抹掉。
        load_user_ai_config(target, strict=True)
    # 对默认 ``~/.topos`` 目录收紧权限；测试/调用方传入相对文件时不要误改
    # 当前工作目录权限。
    if parent != Path("."):
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        # 覆盖前保留一代备份（copy 而非 move：替换失败时原文件仍完好）。
        backup = target.with_name(target.name + ".bak")
        try:
            if target.exists():
                shutil.copy2(target, backup)
                os.chmod(backup, 0o600)
        except OSError as e:
            logger.warning("user AI config backup %s failed: %r", backup, e)
        os.replace(temporary_path, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
