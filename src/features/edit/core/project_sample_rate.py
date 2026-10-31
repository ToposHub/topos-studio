"""M14.3 · 工程 sample-rate 字段迁移（模型层）。

F3.3 的工程级采样率字段：旧工程无键 = 48000 默认（迁移幂等），
44.1k 工程显式声明。序列化迁移经 ``migrate_project_sample_rate``——
读取工程 dict 的既有键（timeline/工程级），无键补默认；不丢未知键。
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

#: 工程默认参考采样率（与 AUDIO_REFERENCE_SAMPLE_RATE 一致）
DEFAULT_PROJECT_SAMPLE_RATE = 48000


def migrate_project_sample_rate(
    project_dict: Dict[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    """工程 dict 的 sample-rate 键迁移（幂等，不改语义）。

    规则：
    - 工程级 ``project_sample_rate`` 键缺失 → 补 48000 默认；
    - 已存在但非法（<=0/非数值）→ 重置默认并标记迁移；
    - 序列级 sample_rate 不动（各序列可异——混合 fps 语义 F3.1 在案）。

    Returns:
        (新 dict, 是否发生迁移)。
    """
    out = dict(project_dict)
    migrated = False
    current = out.get("project_sample_rate")
    try:
        valid = current is not None and int(current) > 0
        value = int(current) if valid else DEFAULT_PROJECT_SAMPLE_RATE
    except (TypeError, ValueError):
        value = DEFAULT_PROJECT_SAMPLE_RATE
        valid = False
    if not valid:
        out["project_sample_rate"] = value
        migrated = current is not None  # 有非法键被修 = 迁移；缺键 = 幂等补默认
    return out, migrated


def project_sample_rate(project: Any) -> int:
    """运行时工程的采样率（无字段 = 默认 48000，不猜）。"""
    value = getattr(project, "project_sample_rate", None)
    try:
        return int(value) if value and int(value) > 0 else (
            DEFAULT_PROJECT_SAMPLE_RATE)
    except (TypeError, ValueError):
        return DEFAULT_PROJECT_SAMPLE_RATE


__all__ = [
    "DEFAULT_PROJECT_SAMPLE_RATE",
    "migrate_project_sample_rate",
    "project_sample_rate",
]
