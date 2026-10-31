"""DESK-07C · 同源分支冲突诊断——绝不用 mtime 自动判定胜负。

场景
----
手机拿走 demo.topos 改了切点；桌面同期也在改。手机把改好的副本拷回
（同名或不同名）。若桌面"打开副本→保存到原路径"，另一分支的修改就
被静默覆盖（.bak 只兜底归档，用户并不知情）。

诊断原则（冻结）
----------------
- **身份**：project_id 相同 → 同一工程的两份拷贝；不同 → 无关工程。
- **内容**：manifest/project.json/edit 主模块的 canonical JSON 摘要。
- **分歧判定**：同 id 且摘要不同 = "diverged"（两个独立演化分支）。
  **不做**"谁更新"的自动裁决——mtime 跨设备不可比（手机/桌面时钟、
  传输工具改写时间戳），只能作为展示信息。处置永远交给人：保留两份 /
  另名保存 / 明确选择一侧。
- revision 单调性仅在**同一分支**内有意义，跨分支比较不构成先后依据。

``overwrite_guard`` 供另存/保存到既有路径前调用：diverged 时返回告警
（UI 必须显式确认，且备份链 .bak 仍然生效——绝不静默覆盖）。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ProjectIdentity",
    "package_identity",
    "classify_divergence",
    "overwrite_guard",
]

#: 参与内容摘要的包内条目（工程语义主体；资源二进制不参与——
#: 同源分歧的判定关心编辑内容，资源由内容寻址天然去重）。
_DIGEST_ENTRIES = (
    "manifest.json",
    "project.json",
    "edit/media_pool.json",
    "edit/bins.json",
    "edit/timelines.json",
    "edit/timeline_sequences.json",
)


@dataclass(frozen=True)
class ProjectIdentity:
    """一个包的身份与内容摘要（诊断输入）。"""

    project_id: str
    revision: int
    modified_at: float
    content_digest: str
    path: str = ""


def _canonical(entry: Any) -> str:
    return json.dumps(
        entry, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False)


def package_identity(path: str) -> ProjectIdentity | None:
    """读取包身份与内容摘要；无法读取返回 None（调用方先行拒绝）。"""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            digest = hashlib.sha256()
            for name in _DIGEST_ENTRIES:
                if name not in names:
                    continue
                digest.update(name.encode())
                digest.update(zf.read(name))
            manifest = json.loads(zf.read("manifest.json"))
            project = json.loads(zf.read("project.json"))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return None
    return ProjectIdentity(
        project_id=str(manifest.get("project_id", "")
                       or project.get("project_id", "")),
        revision=int(project.get("revision", 0) or 0),
        modified_at=float(manifest.get("modified_at", 0.0) or 0.0),
        content_digest=digest.hexdigest(),
        path=path,
    )


def classify_divergence(
    local: ProjectIdentity | None,
    incoming: ProjectIdentity | None,
) -> tuple[str, str]:
    """判定两包关系：返回 (关系, 说明)。

    关系 ∈ identical / unrelated / diverged / unknown。
    "diverged" 不携带方向——双端独立演化，先后只能由人裁决。
    """
    if local is None or incoming is None:
        return "unknown", "包身份不可读（损坏或非 .topos）"
    if local.project_id != incoming.project_id:
        return "unrelated", "不同工程（project_id 不同），无冲突"
    if local.content_digest == incoming.content_digest:
        return "identical", "内容一致（同工程的同一状态）"
    return "diverged", (
        "同一工程的两份独立修改（project_id 相同、内容不同）。"
        "修改时间不可跨设备比较，不做自动裁决——请保留两份或"
        "明确选择一侧；覆盖前会自动备份 .bak")


def overwrite_guard(
    current_source_path: str | None,
    target_path: str,
) -> tuple[str, str]:
    """另存/覆盖前的分支冲突守卫。

    Args:
        current_source_path: 当前打开的工程路径（无则视为新建工程）。
        target_path: 保存目标（已存在的同名工程才可能冲突）。

    Returns:
        (verdict, message)：
        - ``safe``：目标不存在或与当前工程无关/完全一致——常规保存；
        - ``diverged_warning``：目标是同工程的另一分支——UI 必须显式
          确认（.bak 备份仍然生效，绝不静默覆盖）。
    """
    if not current_source_path or not target_path:
        return "safe", ""
    import os

    if not os.path.exists(target_path):
        return "safe", "目标不存在——新文件"
    local = package_identity(current_source_path)
    target = package_identity(target_path)
    relation, message = classify_divergence(local, target)
    if relation == "diverged":
        return "diverged_warning", message
    return "safe", message
