"""Files 领域服务（C 阶段，总计划 C 表第 1/3 行）。

把 B 阶段的 repository + 安全事务包装成**类型化同步服务**：DTO 直接
构造（不经过旧 HTTP 解析），错误用 FilesError/ServiceResult 结构化返回。
C2 的异步 facade 在 worker 里调用这些服务；原生/插件/自动化入口共用
同一实现，不再有第二条业务路径。

分类/展示助手独立实现，不 import 旧 file_explorer。
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterable
from pathlib import Path

from src.features.files.contracts import (
    Availability,
    Entry,
    QueryRequest,
    QueryResult,
    TechnicalMetadata,
    UserMetadata,
    format_duration,
    identity_key,
    not_found_error,
    normalize_path,
    permission_error,
)
from src.features.files.repository.catalog import FilesCatalog, LibraryRecord, nfc
from src.shared.media.format_registry import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

IMAGE_EXTS = IMAGE_EXTENSIONS | {".avif"}
VIDEO_EXTS = VIDEO_EXTENSIONS | {".topos"}
AUDIO_EXTS = AUDIO_EXTENSIONS
DOC_EXTS = {".txt", ".md", ".pdf", ".json", ".xml", ".csv", ".log", ".srt"}

_PAGE_LIMIT = 500


def classify(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in DOC_EXTS:
        return "doc"
    return "other"


class FilesQueryService:
    """目录浏览/树/全库搜索：磁盘为真值，catalog 叠加用户与技术列。"""

    def __init__(self, catalog: FilesCatalog) -> None:
        self._catalog = catalog

    # ------------------------------------------------------------ list

    def list_directory(self, request: QueryRequest, *,
                       library_id: str = "",
                       limit: int = _PAGE_LIMIT,
                       offset: int = 0) -> QueryResult:
        directory = Path(request.path)
        try:
            scanner = os.scandir(directory)
        except FileNotFoundError as exc:
            raise not_found_error(request.path, "目录不存在") from exc
        except PermissionError as exc:
            raise permission_error(request.path, "没有访问权限") from exc
        with scanner as it:
            ordered = sorted((item for item in it if not item.name.startswith(".")),
                             key=lambda item: (nfc(item.name).casefold(), item.name))
        total = len(ordered)
        entries = [self._entry(item, library_id) for item in ordered[offset:offset + limit]]
        truncated = (offset + len(entries)) < total
        return QueryResult(
            request_id=request.request_id, path=request.path,
            entries=tuple(entries), generation=request.generation,
            total=total, truncated=truncated,
            revision=self._catalog.revision,
        )

    def _entry(self, dir_entry: os.DirEntry, library_id: str) -> Entry:
        path = nfc(dir_entry.path)  # DirEntry.path 已含条目名
        try:
            stat = dir_entry.stat(follow_symlinks=False)
            size, mtime = stat.st_size, stat.st_mtime
            availability = Availability.ONLINE
        except OSError:
            size = mtime = None
            availability = Availability.OFFLINE
        is_dir = dir_entry.is_dir(follow_symlinks=False)
        return self._assemble(path, dir_entry.name, is_dir, size, mtime,
                              availability, library_id)

    def _path_entry(self, raw: str, library_id: str) -> Entry:
        """任意路径水合：集合成员/搜索回填等没有 DirEntry 的场合。"""
        path = nfc(raw)
        try:
            stat = os.lstat(path)
            size, mtime = stat.st_size, stat.st_mtime
            availability = Availability.ONLINE
        except OSError:
            size = mtime = None
            availability = Availability.OFFLINE
        name = path.rsplit("/", 1)[-1] or path
        return self._assemble(path, name, os.path.isdir(path), size, mtime,
                              availability, library_id)

    def _assemble(self, path: str, name: str, is_dir: bool,
                  size: int | None, mtime: float | None,
                  availability: str, library_id: str) -> Entry:
        row = self._catalog.asset(library_id, path) if library_id else None
        user = UserMetadata() if not library_id else UserMetadata(
            rating=(row or {}).get("rating") or 0,
            color=(row or {}).get("color") or 0,
            flag=(row or {}).get("flag") or 0,
            tags=tuple(self._catalog.tags_of(library_id, path)),
            comments=(row or {}).get("comment") or "",
        )
        technical = TechnicalMetadata(
            width=(row or {}).get("width"),
            height=(row or {}).get("height"),
            duration_seconds=(row or {}).get("duration"),
            codec=(row or {}).get("codec"),
        )
        return Entry(
            path=path, name=name,
            kind="dir" if is_dir else classify(name),
            is_dir=is_dir, size_bytes=size, mtime_ts=mtime,
            mtime_label=time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            if mtime else "",
            availability=availability, technical=technical, user=user,
        )

    def entries_for_paths(self, paths: Iterable[str], *, request_id: str = "",
                          path_label: str = "", library_id: str = "") -> QueryResult:
        """集合成员/外部路径列表 → 目录条目（磁盘真值 + catalog 叠加）。

        成员已从磁盘消失时保留为 OFFLINE（集合身份仍可解释），不静默丢行。
        """
        entries = tuple(self._path_entry(path, library_id) for path in paths)
        return QueryResult(
            request_id=request_id, path=path_label,
            entries=entries, total=len(entries), truncated=False,
            revision=self._catalog.revision,
        )

    # ------------------------------------------------------------ tree / search

    def tree(self, path: str) -> list[dict[str, str]]:
        root = Path(normalize_path(path))
        try:
            children = sorted(
                (child for child in root.iterdir()
                 if child.is_dir() and not child.name.startswith(".")),
                key=lambda child: child.name.lower(),
            )
        except OSError:
            return []
        return [{"name": child.name, "path": nfc(child)} for child in children]

    def search(self, query: str, *, library_id: str, request_id: str = "",
               limit: int = 200, offset: int = 0) -> QueryResult:
        rows, total = self._catalog.search(
            query, library_id=library_id, limit=limit, offset=offset)
        entries = []
        for row in rows:
            asset = self._catalog.asset(library_id, row["path"]) or {}
            entries.append(Entry(
                path=row["path"], name=os.path.basename(row["path"]),
                kind=classify(row["path"]), size_bytes=asset.get("size"),
                mtime_ts=asset.get("mtime"),
                user=UserMetadata(rating=asset.get("rating") or 0),
            ))
        return QueryResult(
            request_id=request_id, path=f"search://{query}",
            entries=tuple(entries), total=total,
            truncated=(offset + len(entries)) < total,
            revision=self._catalog.revision,
        )

class FilesLibraryService:
    """库注册表服务：固定/取消固定/列表（数据归 catalog.libraries）。"""

    def __init__(self, catalog: FilesCatalog) -> None:
        self._catalog = catalog

    def list_libraries(self) -> tuple[LibraryRecord, ...]:
        return self._catalog.libraries()

    def register(self, root: str | Path, name: str = "") -> LibraryRecord:
        record = self._catalog.register_library(root, name)
        return record

    def unpin(self, library_id: str) -> bool:
        return self._catalog.unregister_library(library_id)

    def library_for_path(self, path: str | Path) -> LibraryRecord | None:
        target = nfc(Path(path).resolve())
        best: LibraryRecord | None = None
        for record in self._catalog.libraries():
            if target == record.root or target.startswith(record.root.rstrip("/") + "/"):
                if best is None or len(record.root) > len(best.root):
                    best = record
        return best
