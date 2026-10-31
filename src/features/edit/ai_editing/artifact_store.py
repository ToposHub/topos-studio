"""ArtifactStore — typed artifact 的存储 + 版本 + 用户优先覆盖（M6 / §15.1 / §11.1 M6 红线）。

镜像 BriefService 的乐观锁 patch 体系，但适用于所有 typed artifact（Script/ShotList/
Storyboard/DeliveryOutline）。

核心不变量（§11.1 M6 红线："用户修改永远优先于模型结果"）：

- ``user_edited=True`` 的 artifact **拒绝** source=MODEL/SKILL 的覆盖（``ArtifactOverrideError``）。
- 模型只能 propose patch（``ArtifactPatch``）；``apply_patch`` 校验 ``base_revision`` 乐观锁。
- 用户编辑走 ``save_user_edit``（标记 user_edited + bump revision）。
- 不写 timeline：artifact 是创作文档，应用需经 plan/approve。

无 Qt 依赖；纯逻辑，可单测。host 负责把 store 接进 app 生命周期（工程关闭可持久化，但
本类不直接 IO——首版纯内存，持久化留 M6 后续接 AssistantSessionStore 模式）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.features.edit.ai_editing.artifact_models import (
    ArtifactKind,
    ArtifactPatch,
    ArtifactSource,
    TypedArtifact,
)

logger = logging.getLogger(__name__)


class ArtifactOverrideError(RuntimeError):
    """用户已编辑的 artifact 被模型结果试图覆盖（§11.1 M6 红线）。"""

    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id
        super().__init__(
            f"artifact {artifact_id!r} 已被用户编辑，拒绝模型/skill 结果覆盖"
            "（§11.1 M6：用户修改永远优先于模型结果）"
        )


class ArtifactRevisionConflictError(RuntimeError):
    """patch 的 base_revision 与当前 artifact 不符（乐观锁冲突）。"""

    def __init__(self, artifact_id: str, base: int, current: int) -> None:
        self.artifact_id = artifact_id
        self.base_revision = base
        self.current_revision = current
        super().__init__(
            f"artifact {artifact_id!r} revision 冲突：patch base={base}, current={current}"
        )


class ArtifactNotFoundError(KeyError):
    """artifact 不存在。"""

    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id
        super().__init__(f"artifact {artifact_id!r} not found")


class ArtifactStore:
    """typed artifact 存储 + 版本 + 用户优先覆盖（M6 / §15.1）。

    内存存储（artifact_id → artifact）；按 kind 查询；patch 乐观锁；用户优先。

    不直接 IO；持久化由宿主接（如 AssistantSessionStore 模式）。
    """

    def __init__(self) -> None:
        self._artifacts: dict[str, TypedArtifact] = {}

    # ---------------- 写入 ---------------- #

    def save(self, artifact: TypedArtifact, *, allow_override: bool = False) -> TypedArtifact:
        """保存一个 artifact（新建或更新）。

        Args:
            allow_override: True 时跳过"用户优先"检查（仅用于用户自身的显式编辑）。
                模型/skill 调用必须 False。

        Raises:
            ArtifactOverrideError: 已有 user_edited=True 的 artifact，新 artifact 的
                source 是 MODEL/SKILL 且 allow_override=False（§11.1 M6 红线）。
        """
        existing = self._artifacts.get(artifact.artifact_id)
        if existing is not None and not allow_override:
            existing_user = getattr(existing, "user_edited", False)
            new_is_machine = (
                getattr(artifact, "source", ArtifactSource.USER)
                in (ArtifactSource.MODEL, ArtifactSource.SKILL)
            )
            if existing_user and new_is_machine:
                raise ArtifactOverrideError(artifact.artifact_id)
        self._artifacts[artifact.artifact_id] = artifact
        return artifact

    def save_user_edit(self, artifact: TypedArtifact) -> TypedArtifact:
        """用户显式编辑：标记 user_edited + bump revision（§11.1 M6 用户优先）。"""
        edited = artifact.with_user_edit().with_revision_bumped()
        # allow_override=True：用户自己的编辑总是允许（即覆盖模型结果）
        return self.save(edited, allow_override=True)

    def apply_patch(self, patch: ArtifactPatch) -> TypedArtifact:
        """应用模型 propose 的 patch（用户批准后调；乐观锁 + 用户优先）。

        Raises:
            ArtifactNotFoundError: artifact 不存在。
            ArtifactRevisionConflictError: base_revision 不符。
            ArtifactOverrideError: artifact 已被用户编辑。
        """
        existing = self._artifacts.get(patch.artifact_id)
        if existing is None:
            raise ArtifactNotFoundError(patch.artifact_id)
        if existing.user_edited:
            # 用户改过 → 拒绝模型 patch（§11.1 M6 红线）
            raise ArtifactOverrideError(patch.artifact_id)
        if patch.base_revision != existing.revision:
            raise ArtifactRevisionConflictError(
                patch.artifact_id, patch.base_revision, existing.revision,
            )
        # merge：把 patch.changes 字段级 merge 进 existing，bump revision
        merged = self._merge_patch(existing, patch)
        # patch 来自 model/skill → source 标记，但 user_edited 仍为 False（用户未手改）
        self._artifacts[patch.artifact_id] = merged
        return merged

    # ---------------- 读取 ---------------- #

    def get(self, artifact_id: str) -> TypedArtifact | None:
        return self._artifacts.get(artifact_id)

    def require(self, artifact_id: str) -> TypedArtifact:
        """取 artifact；不存在抛 ArtifactNotFoundError。"""
        a = self._artifacts.get(artifact_id)
        if a is None:
            raise ArtifactNotFoundError(artifact_id)
        return a

    def list_by_kind(self, kind: ArtifactKind) -> list[TypedArtifact]:
        """按 kind 列出 artifact（UI 编辑卡分组用）。"""
        return [a for a in self._artifacts.values() if getattr(a, "kind", None) == kind]

    def list_all(self) -> list[TypedArtifact]:
        return list(self._artifacts.values())

    def remove(self, artifact_id: str) -> bool:
        return self._artifacts.pop(artifact_id, None) is not None

    @property
    def count(self) -> int:
        return len(self._artifacts)

    # ---------------- 内部 ---------------- #

    @staticmethod
    def _merge_patch(existing: TypedArtifact, patch: ArtifactPatch) -> TypedArtifact:
        """把 patch.changes 字段级 merge 进 existing，bump revision。

        changes 的 key 必须是 existing 上的合法字段；未知字段忽略（防注入）。
        """
        from dataclasses import fields, replace

        valid_fields = {f.name for f in fields(existing)}
        merged_changes: dict[str, Any] = {}
        for k, v in patch.changes.items():
            if k in valid_fields and k not in ("artifact_id", "kind", "revision"):
                merged_changes[k] = v
        # bump revision；记录来源 + 时间
        merged_changes["revision"] = existing.revision + 1
        if patch.source in (ArtifactSource.MODEL, ArtifactSource.SKILL):
            merged_changes["source"] = patch.source
        merged_changes["created_at"] = str(time.time())
        return replace(existing, **merged_changes)


__all__ = [
    "ArtifactStore",
    "ArtifactOverrideError",
    "ArtifactRevisionConflictError",
    "ArtifactNotFoundError",
]
