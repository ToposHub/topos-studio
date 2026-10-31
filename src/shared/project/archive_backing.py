"""DESK-03A · ArchiveBackingStore：加载后持有来源包载荷，另存不丢模块。

问题
----
``ToposProjectIO.save`` 的 Phase B 只从**目标路径**读已有包来保留未知
模块。于是：

- 另存到新目录/新文件名 → 目标不存在 → 空基底 → 来源包里其他产品的
  模块、AI 载荷、内嵌资源全部丢失；
- 自动保存到 ``.autosave``（首次或被清理后）→ 同理丢；
- 保存前 manifest 的溯源字段（created_at/created_by）与 never-downgrade
  版本守卫也只看目标包——另存即丢溯源。

设计
----
``load`` 成功后由调用方（EditProjectIO）捕获来源包的
``ProjectArchivePayload``（资源二进制惰性引用，不驻内存），
``save`` 时传入 ``backing_store``：

- 目标路径 == 来源路径（原地保存）→ 不参与（目标本身就是完整状态）；
- 目标 ≠ 来源（另存/复制/自动保存）→ backing 作为**保留基底**先并入
  目标 payload，**目标已有条目优先**（目标代表更新的编辑状态），
  backing 只补缺——不会复活目标里被显式清空的模块。

失败语义：backing 的 preserved_members 在写包时从来源文件流式读取；
来源包被移动/删除 → ``ProjectArchiveReadError`` → 保存受控失败，
绝不静默丢资源。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

from .payload_cache import ProjectArchivePayload

logger = logging.getLogger(__name__)

__all__ = ["ArchiveBackingStore", "merge_backing_payload"]


@dataclass(frozen=True)
class ArchiveBackingStore:
    """来源 ``.topos`` 包的载荷快照。

    Attributes:
        source_path: 来源包路径（preserved_members 流式回读的锚点）。
        payload: 来源包全量载荷（大资源为惰性引用）。
        captured_at: 捕获时间（诊断用）。
    """

    source_path: str
    payload: ProjectArchivePayload
    captured_at: float = field(default_factory=time.time)

    @classmethod
    def capture(cls, source_path: str) -> "ArchiveBackingStore | None":
        """捕获来源包载荷；文件不存在返回 None（新工程/已被移动）。

        Raises:
            ProjectArchiveReadError: 来源包存在但不可读。调用方（load
                刚成功）可选择降级为无 backing 继续工作，但必须留日志。
        """
        if not source_path or not os.path.exists(source_path):
            return None
        payload = ProjectArchivePayload.load_from_file(
            source_path, defer_assets=True)
        return cls(
            source_path=os.path.abspath(source_path),
            payload=payload,
        )

    def is_same_target(self, filepath: str) -> bool:
        """目标是否就是来源本身（原地保存：backing 不参与合并）。"""
        return os.path.abspath(filepath) == self.source_path


def merge_backing_payload(
    destination: ProjectArchivePayload,
    backing: ProjectArchivePayload,
) -> ProjectArchivePayload:
    """把 backing 并入 destination（**目标优先，backing 补缺**）。

    合并维度：module_payloads / unknown_files / preserved_members /
    file_members / manifest / project_json。已存在于目标的键不动——
    显式清空（空 dict 覆写）不会被旧值复活。
    """
    for name, data in backing.module_payloads.items():
        if name not in destination.module_payloads:
            destination.module_payloads[name] = data
    for name, raw in backing.unknown_files.items():
        if name not in destination.unknown_files:
            destination.unknown_files[name] = raw
    for name, member in backing.preserved_members.items():
        if name not in destination.preserved_members:
            destination.preserved_members[name] = member
    for name, member in backing.file_members.items():
        if name not in destination.file_members:
            destination.file_members[name] = member
    if not destination.manifest and backing.manifest:
        # 溯源与版本守卫的基底（save 随后仍会以新 manifest 覆写合并）
        destination.manifest = backing.manifest
    if not destination.project_json and backing.project_json:
        destination.project_json = backing.project_json
    return destination
