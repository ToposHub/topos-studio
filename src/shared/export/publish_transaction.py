"""M21.1b · 多文件发布状态机（编码 + sidecar + manifest 成组发布）。

单文件原子性由编码器发布事务（M21.1a）保证；本模块处理**成组**文件
（成片 + sidecar 谱文件 + manifest）的一致发布：任一文件失败/取消
→ 整组回滚（旧成功产物逐字节保留），多文件不假设一次 rename 全原子。

复用 VerifiedFrameManifest/manifest json 落盘约定；不另建平行缓存。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class PublishFile:
    """一个待发布文件（temp → final）。"""

    temp_path: str
    final_path: str
    kind: str = "media"   # media / sidecar / manifest


@dataclass(frozen=True)
class PublishResult:
    ok: bool
    published: Tuple[str, ...] = ()
    error: str = ""
    stage: str = ""       # backup / move / commit


def publish_file_group(
    files: List[PublishFile],
    *,
    backup_suffix: str = ".topos-old",
) -> PublishResult:
    """成组发布：备份旧→新就位→（调用方在成功后清备份）。

    阶段：
    1. **backup**——组内每个 final 已存在者整体移到 ``<final><suffix>``；
    2. **move**——逐文件 ``os.replace(temp, final)``；
    3. 任一步失败 → **回滚**：已 move 的新文件移回 temp（恢复备份），
       已备份的旧文件复位——旧成功产物保持完整。

    manifest 文件建议排在组末（``kind="manifest"`` 作为提交标记）。
    成功后调用方删除 ``<final><suffix>`` 备份（只动本组文件）。
    """
    if not files:
        return PublishResult(ok=False, error="空发布组", stage="backup")
    backed_up: List[Tuple[str, str]] = []   # (backup_path, final_path)
    moved: List[PublishFile] = []
    try:
        for f in files:
            if os.path.exists(f.final_path):
                backup = f.final_path + backup_suffix
                os.replace(f.final_path, backup)
                backed_up.append((backup, f.final_path))
        for f in files:
            if not os.path.exists(f.temp_path):
                raise OSError(f"临时文件缺失：{f.temp_path}")
            os.replace(f.temp_path, f.final_path)
            moved.append(f)
    except OSError as exc:
        # 回滚：新就位文件退回 temp；旧备份复位
        for f in moved:
            try:
                os.replace(f.final_path, f.temp_path)
            except OSError:
                pass
        for backup, final in backed_up:
            try:
                os.replace(backup, final)
            except OSError:
                pass
        return PublishResult(
            ok=False,
            error=f"发布失败（磁盘满/占用？）：{exc}",
            stage="backup" if not moved else "move")
    return PublishResult(
        ok=True, published=tuple(f.final_path for f in files))


def cleanup_group_backups(
    files: List[PublishFile],
    *,
    backup_suffix: str = ".topos-old",
) -> int:
    """发布成功后清理组内备份（只动本组文件的备份）。"""
    removed = 0
    for f in files:
        backup = f.final_path + backup_suffix
        try:
            if os.path.exists(backup):
                os.remove(backup)
                removed += 1
        except OSError:
            pass
    return removed


__all__ = ["PublishFile", "PublishResult",
           "publish_file_group", "cleanup_group_backups"]
