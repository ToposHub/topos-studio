"""G04：Gallery 集合 scope 规格、PowerGrade 存储与虚拟 Timelines 集合。

写明的 scope 规格（计划 §5 G04.1——先规格后启用）：

============= ================== ================== =====================
scope         生命周期           写入               现状
============= ================== ================== =====================
project       随工程打开/关闭     工程内 album      **未实现**（无存储
                                                     服务，不伪造）
global        独立于工程         用户全局可复用     PowerGrade＝真实
                                                     文件夹存储（本模块）
legacy-folder 应用数据目录       既有 User/System   G01 起映射为明确
                                 平铺目录           的 legacy-folder
                                                     album，原路径可读
============= ================== ================== =====================

- **不迁移**：System 不自动搬进 PowerGrade、不重命名伪造 scope；旧
  User/System 数据原路径继续可读。PowerGrade 文件夹只在用户显式
  ``ensure_powergrade_album`` 时创建（可回退＝空目录删除）。
- **虚拟 Timelines 集合**：按 still metadata 的 ``timeline_id`` 快照
  聚合的**只读**分组；身份用 timeline_id（工程/timeline 同名不串项），
  label 是抓帧时的 timeline 名快照；timeline 在当前工程已删除 →
  ``offline`` 标记。虚拟集合不可写入——Capture 需要明确可写目标
  album（legacy/global），不从 timeline 名拼目录。
- project scope：规格在、服务未建——``list_albums`` 永不返回 project
  scope 的 album，能力层保持 False＋稳定 reason（G01 注册表）。
- Memories：另行建模（独立模型/存储），本模块不提供。
"""

from __future__ import annotations

import os
from typing import Dict, FrozenSet, Iterable, Optional, Tuple

from src.features.color.core.gallery_contracts import (
    ALBUM_KIND_POWERGRADE,
    ALBUM_KIND_STILLS,
    ALBUM_KIND_TIMELINE,
    GalleryAlbum,
    GalleryOperationResult,
    GalleryQuery,
    GalleryStill,
    OP_SUCCESS,
    SCOPE_GLOBAL,
    make_gallery_request_id,
)

#: PowerGrade 的真实存储目录（位于 Gallery 根下；存在即全局 scope 生效）
POWERGRADE_DIR_NAME = "PowerGrade"

#: 虚拟 timeline 集合的 album_id 前缀（与 legacy: 区分）
VIRTUAL_TIMELINE_PREFIX = "virtual-timeline:"

# 稳定 reason
REASON_ALREADY_EXISTS = "already_exists"
REASON_DIR_CREATE_FAILED = "dir_create_failed"
REASON_DIR_NOT_EMPTY = "dir_not_empty"


class GalleryAlbumScopeService:
    """集合 scope 的单一事实源（读侧聚合；无 Qt）。"""

    def __init__(self, repository) -> None:
        self._repository = repository

    @property
    def _root(self) -> str:
        return self._repository.root

    # ── PowerGrade（global scope，真实文件夹存储） ─────────────────────

    def powergrade_folder(self) -> str:
        return os.path.join(self._root, POWERGRADE_DIR_NAME)

    def has_powergrade_storage(self) -> bool:
        return os.path.isdir(self.powergrade_folder())

    def ensure_powergrade_album(self) -> GalleryOperationResult:
        """显式创建 PowerGrade 存储（可审阅、可回退；不动 System）。

        回退＝目录为空时 ``remove_powergrade_album``；已有用户数据后
        回退拒绝（不自动删除任何资产）。
        """
        request_id = make_gallery_request_id()
        folder = self.powergrade_folder()
        if os.path.isdir(folder):
            return GalleryOperationResult(
                request_id=request_id, status="rejected",
                reason=REASON_ALREADY_EXISTS, warnings=(folder,))
        try:
            os.makedirs(folder, exist_ok=False)
        except OSError as exc:
            return GalleryOperationResult(
                request_id=request_id, status="rejected",
                reason=REASON_DIR_CREATE_FAILED, warnings=(str(exc),))
        return GalleryOperationResult(
            request_id=request_id, status=OP_SUCCESS,
            affected_still_ids=(), warnings=(folder,))

    def remove_powergrade_album(self) -> GalleryOperationResult:
        """回退（仅空目录）；有用户数据时如实拒绝。"""
        request_id = make_gallery_request_id()
        folder = self.powergrade_folder()
        if not os.path.isdir(folder):
            return GalleryOperationResult(
                request_id=request_id, status="rejected",
                reason=REASON_DIR_NOT_EMPTY, warnings=("absent",))
        try:
            entries = os.listdir(folder)
        except OSError as exc:
            return GalleryOperationResult(
                request_id=request_id, status="rejected",
                reason=REASON_DIR_CREATE_FAILED, warnings=(str(exc),))
        if entries:
            return GalleryOperationResult(
                request_id=request_id, status="rejected",
                reason=REASON_DIR_NOT_EMPTY,
                warnings=(f"{len(entries)} entries",))
        try:
            os.rmdir(folder)
        except OSError as exc:
            return GalleryOperationResult(
                request_id=request_id, status="rejected",
                reason=REASON_DIR_CREATE_FAILED, warnings=(str(exc),))
        return GalleryOperationResult(request_id=request_id,
                                      status=OP_SUCCESS)

    # ── album 聚合（legacy ＋ global；project 永不伪造） ───────────────

    def list_albums(self) -> Tuple[GalleryAlbum, ...]:
        albums: list[GalleryAlbum] = []
        albums.extend(self._repository.list_albums())
        if self.has_powergrade_storage():
            albums.append(GalleryAlbum(
                album_id="global:powergrade",
                scope=SCOPE_GLOBAL,
                kind=ALBUM_KIND_POWERGRADE,
                label="PowerGrade",
                read_only=not os.access(self.powergrade_folder(),
                                        os.W_OK),
                revision=self._folder_revision(self.powergrade_folder()),
            ))
        # 工程关闭/切换不影响本列表（global 独立、legacy 在应用数据）；
        # project scope 无存储服务——绝不在此返回 project album。
        return tuple(albums)

    def album_by_id(self, album_id: str) -> Optional[GalleryAlbum]:
        for album in self.list_albums():
            if album.album_id == album_id:
                return album
        return None

    # ── 虚拟 Timelines 集合（只读聚合；写入一律走真实 album） ──────────

    def library_stills(self) -> Tuple[GalleryStill, ...]:
        """库内全部 stills（legacy 各 album ＋ PowerGrade）。"""
        stills: list[GalleryStill] = []
        for album in self.list_albums():
            if album.kind == ALBUM_KIND_POWERGRADE:
                result = self._repository.query_folder(
                    self.powergrade_folder(),
                    GalleryQuery(album_id=album.album_id),
                    album=album, album_id=album.album_id)
            else:
                result = self._repository.query(GalleryQuery(
                    album_id=album.album_id))
            stills.extend(result.stills)
        return tuple(stills)

    def timeline_collections(self, project_timeline_ids: Optional[
            FrozenSet[str]] = None) -> Tuple[GalleryAlbum, ...]:
        """按 timeline_id 快照聚合的只读虚拟集合。

        ``project_timeline_ids`` 提供时用于离线判定（timeline 已从当前
        工程删除 → ``timeline_id=None`` 快照仍可展示、标记 offline）。
        身份键是 timeline_id：两个工程的同名 timeline 不串项；label 用
        抓帧时的 timeline_name 快照（timeline 改名不影响身份）。
        """
        groups: Dict[str, Dict[str, Optional[object]]] = {}
        for still in self.library_stills():
            timeline_id = still.provenance.timeline_id
            if not timeline_id:
                continue
            group = groups.setdefault(timeline_id, {
                "name": still.provenance.timeline_name or timeline_id,
                "revision": 0, "count": 0})
            group["count"] += 1
            if still.provenance.timeline_name:
                group["name"] = still.provenance.timeline_name
            group["revision"] = max(group["revision"],
                                    still.content_revision)
        albums: list[GalleryAlbum] = []
        for timeline_id, group in sorted(groups.items(),
                                         key=lambda kv: str(kv[1]["name"])):
            offline = (project_timeline_ids is not None
                       and timeline_id not in project_timeline_ids)
            albums.append(GalleryAlbum(
                album_id=f"{VIRTUAL_TIMELINE_PREFIX}{timeline_id}",
                scope="global",          # 虚拟集合：跨工程展示，只读
                kind=ALBUM_KIND_TIMELINE,
                label=str(group["name"]),
                read_only=True,          # 虚拟集合不可写入
                timeline_id=timeline_id,
                revision=int(group["revision"]),
                offline=offline,
            ))
        return tuple(albums)

    def stills_for_timeline(self, timeline_id: str
                            ) -> Tuple[GalleryStill, ...]:
        """某虚拟 timeline 集合下的 stills（跨 album 聚合，稳定排序）。"""
        matched = [still for still in self.library_stills()
                   if still.provenance.timeline_id == timeline_id]
        matched.sort(key=lambda s: (s.created_at or "",
                                    s.still_id or ""))
        return tuple(matched)

    @staticmethod
    def _folder_revision(folder: str) -> int:
        try:
            return os.stat(folder).st_mtime_ns
        except OSError:
            return 0


def is_timeline_virtual_album(album_id: str) -> bool:
    return album_id.startswith(VIRTUAL_TIMELINE_PREFIX)


def timeline_id_of_virtual_album(album_id: str) -> Optional[str]:
    if not is_timeline_virtual_album(album_id):
        return None
    return album_id[len(VIRTUAL_TIMELINE_PREFIX):] or None
