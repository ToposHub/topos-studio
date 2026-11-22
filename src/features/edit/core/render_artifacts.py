"""B21 · Printmaster / Freeze / Bounce 统一产物模型（主计划 §16）。

三者共用同一渲染核心（不新增混音算法），区别在产物语义：

- ``freeze``：性能缓存——引用原链完整快照，可解冻回原链；依赖变化即过期。
- ``bounce``：生成可追溯的新 asset/clip（替换/关联来源需一次 command）。
- ``printmaster``：固定 revision 的交付打印——作业期间 revision 锁定，
  UI 继续编辑生成新 revision 不影响正在输出的旧快照。

本模块定义不可变 ``RenderArtifact`` 与注册表（JSON sidecar、原子写），
以及失效判定：声音身份（sound hash）或依赖签名（效果/路由/自动化/侧链
源）任一变化 → stale；跨工程使用的成品是正式 asset，不由缓存清理删除。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

ARTIFACT_META_VERSION = 1
ARTIFACT_KINDS = ("freeze", "bounce", "printmaster")
ARTIFACT_STATUSES = ("pending", "ready", "failed", "cancelled")


@dataclass(frozen=True)
class RenderArtifact:
    """一次渲染作业的产物记录（不可变；元数据进工程 sidecar，PCM 不进）。"""

    artifact_id: str
    kind: str                       # freeze | bounce | printmaster
    project_id: str
    sequence_id: str
    revision: int                   # 作业锁定的工程 revision
    sound_hash: str                 # 渲染输入的声音身份（B01 投影）
    start_sample: int = 0
    sample_count: int = 0
    sample_rate: int = 48000
    channel_order: Tuple[str, ...] = ("L", "R")
    tap: str = "master"             # master / bus:<id> / track:<id>
    pdc_total_samples: int = 0
    tail_policy: str = "drain"      # drain | strict_range
    # 依赖签名（效果参数/路由/自动化/侧链源的稳定指纹集合）
    source_dependencies: Tuple[str, ...] = ()
    file_uri: str = ""
    file_sha256: str = ""
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in ARTIFACT_KINDS:
            raise ValueError(f"kind must be one of {ARTIFACT_KINDS}: {self.kind!r}")
        if self.status not in ARTIFACT_STATUSES:
            raise ValueError(
                f"status must be one of {ARTIFACT_STATUSES}: {self.status!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "meta_version": ARTIFACT_META_VERSION,
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "project_id": self.project_id,
            "sequence_id": self.sequence_id,
            "revision": self.revision,
            "sound_hash": self.sound_hash,
            "start_sample": self.start_sample,
            "sample_count": self.sample_count,
            "sample_rate": self.sample_rate,
            "channel_order": list(self.channel_order),
            "tap": self.tap,
            "pdc_total_samples": self.pdc_total_samples,
            "tail_policy": self.tail_policy,
            "source_dependencies": list(self.source_dependencies),
            "file_uri": self.file_uri,
            "file_sha256": self.file_sha256,
            "status": self.status,
            "created_at": self.created_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RenderArtifact":
        return cls(
            artifact_id=str(data.get("artifact_id", "")),
            kind=str(data.get("kind", "freeze")),
            project_id=str(data.get("project_id", "")),
            sequence_id=str(data.get("sequence_id", "")),
            revision=int(data.get("revision", 0) or 0),
            sound_hash=str(data.get("sound_hash", "")),
            start_sample=int(data.get("start_sample", 0) or 0),
            sample_count=int(data.get("sample_count", 0) or 0),
            sample_rate=int(data.get("sample_rate", 48000)),
            channel_order=tuple(data.get("channel_order", ("L", "R")) or ("L", "R")),
            tap=str(data.get("tap", "master")),
            pdc_total_samples=int(data.get("pdc_total_samples", 0) or 0),
            tail_policy=str(data.get("tail_policy", "drain")),
            source_dependencies=tuple(
                str(d) for d in (data.get("source_dependencies") or ())),
            file_uri=str(data.get("file_uri", "")),
            file_sha256=str(data.get("file_sha256", "")),
            status=str(data.get("status", "pending")),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            error=data.get("error"),
        )


def freeze_staleness(
    artifact: RenderArtifact,
    *,
    current_sound_hash: str,
    current_dependencies: Optional[Tuple[str, ...]] = None,
    file_sha256: str = "",
) -> Optional[str]:
    """冻结产物失效判定；返回失效原因（None=仍新鲜）。

    规则（§16 步骤 4）：源重连/效果或自动化变化、sidechain 源变化、
    Bus 上游和 route 变化都会改变依赖签名或声音身份 → 过期。failed/
    cancelled/pending 不是可用产物；文件 hash 不一致（成品文件被改/
    损坏）同样过期。
    """
    if artifact.status != "ready":
        return f"artifact status not usable: {artifact.status}"
    if current_sound_hash and artifact.sound_hash != current_sound_hash:
        return (f"sound hash changed: {artifact.sound_hash[:12]}… → "
                f"{current_sound_hash[:12]}…")
    if current_dependencies is not None and set(current_dependencies) != set(
            artifact.source_dependencies):
        return "source dependencies changed"
    if file_sha256 and artifact.file_sha256 != file_sha256:
        return "artifact file content changed"
    return None


class PrintJobLock:
    """Printmaster 作业 revision 锁（§16 步骤 7）。

    打印作业开始时锁定 revision 与 sound hash；渲染期间 UI 继续编辑产生
    新 revision 不影响作业（输出仍按锁定快照完成）；界面可读取本次输出
    的 revision 报告给用户。
    """

    def __init__(self, revision: int, sound_hash: str) -> None:
        self._revision = int(revision)
        self._sound_hash = str(sound_hash)
        self._finished = False

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def sound_hash(self) -> str:
        return self._sound_hash

    @property
    def finished(self) -> bool:
        return self._finished

    def verify_input(self, sound_hash: str) -> bool:
        """渲染输入是否仍与锁定快照一致（不一致应中止作业，不得混音两版）。"""
        return (not self._finished) and sound_hash == self._sound_hash

    def finish(self) -> None:
        self._finished = True


class ArtifactRegistry:
    """产物注册表（工程 sidecar JSON；原子读写；PCM 不进工程文件）。"""

    def __init__(self, project_dir: str | Path) -> None:
        self._dir = Path(project_dir) / "render_artifacts"
        self._artifacts: Dict[str, RenderArtifact] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._dir / "artifacts.json"

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            for item in payload.get("artifacts", []):
                artifact = RenderArtifact.from_dict(item)
                self._artifacts[artifact.artifact_id] = artifact
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — 损坏 sidecar 不阻断工程打开
            logger.warning("render artifacts sidecar unreadable: %s",
                           self.path, exc_info=True)

    def _save(self, *, upserts: Tuple[RenderArtifact, ...] = (),
              drop_ids: Tuple[str, ...] = ()) -> bool:
        """Apply only this transaction's changes under the shared file lock.

        A loaded registry is a read cache, never an authoritative replacement
        for the disk table. Failed writes leave both disk and cache unchanged.
        Unknown sidecar/record fields survive edits of other records.
        """
        from src.shared.plugins.install_lock import create_lock

        self._dir.mkdir(parents=True, exist_ok=True)
        lock = create_lock(self._dir / "artifacts.lock")
        try:
            lock.acquire()
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                payload = {"meta_version": ARTIFACT_META_VERSION, "artifacts": []}
            if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
                raise ValueError("invalid render artifacts sidecar")
            if payload.get("meta_version", ARTIFACT_META_VERSION) != ARTIFACT_META_VERSION:
                raise ValueError("unsupported render artifacts metadata version")
            records: Dict[str, Dict[str, Any]] = {}
            for item in payload["artifacts"]:
                artifact = RenderArtifact.from_dict(item)
                records[artifact.artifact_id] = dict(item)
            removed = any(artifact_id in records for artifact_id in drop_ids)
            for artifact_id in drop_ids:
                records.pop(artifact_id, None)
            for artifact in upserts:
                record = dict(records.get(artifact.artifact_id, {}))
                record.update(artifact.to_dict())
                records[artifact.artifact_id] = record
            payload["artifacts"] = [records[key] for key in sorted(records)]
            data = json.dumps(payload, ensure_ascii=False, indent=2,
                              allow_nan=False) + "\n"
            fd, tmp_name = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                raise
            self._artifacts = {
                key: RenderArtifact.from_dict(record) for key, record in records.items()
            }
            self._loaded = True
            return removed
        finally:
            lock.close()

    def register(self, artifact: RenderArtifact) -> RenderArtifact:
        self._save(upserts=(artifact,))
        return artifact

    def get(self, artifact_id: str) -> Optional[RenderArtifact]:
        self._ensure_loaded()
        return self._artifacts.get(artifact_id)

    def list(self, kind: Optional[str] = None) -> Tuple[RenderArtifact, ...]:
        self._ensure_loaded()
        return tuple(
            a for a in sorted(self._artifacts.values(),
                              key=lambda a: a.created_at)
            if kind is None or a.kind == kind
        )

    def remove(self, artifact_id: str) -> bool:
        return self._save(drop_ids=(artifact_id,))


__all__ = [
    "ARTIFACT_KINDS",
    "ARTIFACT_META_VERSION",
    "ARTIFACT_STATUSES",
    "ArtifactRegistry",
    "PrintJobLock",
    "RenderArtifact",
    "freeze_staleness",
]
