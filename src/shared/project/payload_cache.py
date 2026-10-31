"""
ProjectArchivePayload - Raw payload cache for preserving unknown module data during save.

When reading a .topos ZIP, ALL entries are captured into this cache.
During save, the current product overwrites only its own modules,
and everything else is written back unchanged.

This prevents one product from accidentally deleting module data
that belongs to another product (e.g., topos-color deleting edit timeline data).

Large ``assets/`` members are preserved lazily (AUD-36901): instead of
reading every binary asset into memory, the payload records a reference to
the source archive and streams the member directly into the destination
ZIP at write time. The streamed members are written without re-compression
(``ZIP_STORED``) so repeated saves do not re-deflate megabytes of media
data on every save.
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Known JSON files inside the archive mapped to their logical keys.
# Anything not in this set is treated as an unknown file.
KNOWN_JSON_FILES: dict[str, str] = {
    "manifest.json": "manifest",
    "project.json": "project_json",
    "edit/media_pool.json": "edit/media_pool",
    "edit/bins.json": "edit/bins",
    "edit/timelines.json": "edit/timelines",
    "edit/timeline_sequences.json": "edit/timeline_sequences",
    # 媒体池 Comp Clip 元数据（CompClipData），丢失会导致合成重开后消失。
    "edit/comp_clips.json": "edit/comp_clips",
    "color/color_stacks.json": "color/color_stacks",
    "color/looks.json": "color/looks",
    "composite/graphs.json": "composite/graphs",
    "composite/viewer_state.json": "composite/viewer_state",
    "deliver/presets.json": "deliver/presets",
    "ui/workspace_state.json": "ui/workspace_state",
    "cache_manifest.json": "cache_manifest",
    # AI 编辑子系统（TAEP）payload：会话目录 + plan 仓库。
    # 显式登记确保 Phase B 往返保留 + load 侧能读到。
    "ai/session.json": "ai/session",
    "ai/session_catalog.json": "ai/session_catalog",
    "ai/plans.json": "ai/plans",
    "ai/jobs.json": "ai/jobs",
    "ai/analysis.json": "ai/analysis",
    "ai/runtime.json": "ai/runtime",
    "ai/trace.json": "ai/trace",
    # Optional Teaching Animation payload.  Keeping this path known makes the
    # fallback explicit while the generic payload cache still preserves any
    # future scene/cache files byte-for-byte.
    "teaching_animation/scenes.json": "teaching_animation/scenes",
    "teaching_animation/render_cache_manifest.json": "teaching_animation/render_cache_manifest",
    # AI Workflow 存储（用户编辑的节点图，plan §13）。
    "ai_workflow/workflows.json": "ai_workflow/workflows",
    # DESK-01B：内嵌资源索引（资源协议见 docs/adr/0005 §3）。登记为已知
    # JSON 使索引随 Phase B 往返保留，且 load 侧可读取。
    "resources/resources.json": "resources/index",
}

#: Members under this prefix are treated as large binary assets and are the
#: candidates for lazy (streamed) preservation.
ASSETS_PREFIX = "assets/"

#: DESK-01C：内嵌资源（原片/字体/LUT）与 assets/ 同属大二进制——读取时
#: 同样惰性保留（只记来源引用，写回时流式拷贝），10 GB 包不整体进内存。
RESOURCE_FILES_PREFIX = "resources/"

#: 资源索引 JSON 的归档路径（唯一事实来源；resource_resolver 由此导入）。
RESOURCE_INDEX_ARCHIVE_PATH = "resources/resources.json"

#: Compression applied to lazily preserved asset members on write-back.
#: Asset payloads (EXR/PNG/JPEG/proxies) are media data that barely deflate;
#: re-compressing them on every save costs CPU for negligible size gains
#: (AUD-36901), so they are copied through verbatim.
PRESERVED_ASSET_COMPRESS_TYPE = zipfile.ZIP_STORED

#: Chunk size when streaming preserved members between archives.
_STREAM_COPY_CHUNK = 1024 * 1024


class ProjectArchiveReadError(Exception):
    """An existing .topos archive could not be read for payload preservation.

    Raised instead of returning an empty payload (AUD-36902): a caller that
    saves with an empty payload would silently drop every other product's
    module data from the archive.
    """


@dataclass(frozen=True)
class _PreservedMember:
    """Reference to a source-archive member preserved without loading it.

    Attributes:
        source_path: Filesystem path of the archive holding the member.
        filename: Archive-internal member name.
        date_time: Original modification timestamp for the copy.
        file_size: Uncompressed size (diagnostics / sanity check).
    """

    source_path: str
    filename: str
    date_time: tuple[int, int, int, int, int, int]
    file_size: int


@dataclass(frozen=True)
class _FileMember:
    """A plain filesystem file to be streamed into the destination archive.

    DESK-01B：内嵌资源（原片/字体/LUT）写入包时的零内存通道——写入时
    流式拷贝，不整体驻留内存（10 GB 纪律）。
    """

    source_path: str
    filename: str
    date_time: tuple[int, int, int, int, int, int]
    file_size: int


@dataclass
class ProjectArchivePayload:
    """
    Raw payload cache for preserving unknown module data during save.

    Attributes:
        manifest: Raw manifest.json dict (may be empty for new projects).
        project_json: Raw project.json dict (may be empty for new projects).
        module_payloads: Mapping of archive path -> parsed JSON dict for
            all known module JSON files.
        unknown_files: Mapping of archive path -> raw bytes for files
            that are not recognized (non-JSON, future modules, etc.).
        preserved_members: Mapping of archive path -> lazy reference for
            large ``assets/`` members (AUD-36901). Populated only by
            :meth:`load_from_file` / ``read_from_zip(defer_assets=True)``;
            written back by streaming from the source archive.
    """

    manifest: dict[str, Any] = field(default_factory=dict)
    project_json: dict[str, Any] = field(default_factory=dict)
    module_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    unknown_files: dict[str, bytes] = field(default_factory=dict)
    preserved_members: dict[str, _PreservedMember] = field(default_factory=dict)
    #: DESK-01B：{archive_path: 本机源文件}。写包时流式拷入（ZIP_STORED，
    #: 媒体数据再压缩无收益），不整读进内存；同路径的新内容覆盖
    #: preserved/unknown 里的旧条目。
    file_members: dict[str, _FileMember] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @staticmethod
    def read_from_zip(
        zf: zipfile.ZipFile,
        *,
        defer_assets: bool = False,
        source_path: str | None = None,
    ) -> ProjectArchivePayload:
        """
        Read ALL entries from an open ZipFile into a new payload.

        JSON files are parsed; everything else is stored as raw bytes.
        Parse failures for JSON files fall back to raw bytes.

        Args:
            zf: Open ZipFile (read mode).
            defer_assets: When True, ``assets/`` members are NOT read into
                memory; they are recorded in ``preserved_members`` and
                streamed back at write time (AUD-36901). Requires a
                re-openable *source_path*; falls back to eager reads when
                the archive was opened from a stream.
            source_path: Explicit source path for deferred members. When
                omitted, ``zf.filename`` is used if it is a string.
        """
        payload = ProjectArchivePayload()

        if defer_assets and source_path is None:
            candidate = getattr(zf, "filename", None)
            source_path = candidate if isinstance(candidate, str) else None
        if defer_assets and not source_path:
            # No re-openable source: keep the payload complete by reading
            # eagerly (compatibility with stream-opened archives).
            defer_assets = False

        for info in zf.infolist():
            if info.is_dir():
                continue

            name = info.filename

            # Skip assets directory entries (large binary data).
            # AUD-36901: on save paths they are preserved lazily so memory
            # does not scale with total asset size.
            # DESK-01C: embedded resources under resources/ get the same
            # lazy treatment (media payloads, same size class). The index
            # JSON itself is exempt — it is a known module parsed below.
            if name.startswith((ASSETS_PREFIX, RESOURCE_FILES_PREFIX)) \
                    and name != RESOURCE_INDEX_ARCHIVE_PATH:
                if defer_assets and source_path:
                    payload.preserved_members[name] = _PreservedMember(
                        source_path=source_path,
                        filename=name,
                        date_time=info.date_time,
                        file_size=info.file_size,
                    )
                else:
                    payload.unknown_files[name] = zf.read(name)
                continue

            if name == "manifest.json":
                raw = zf.read(name)
                try:
                    payload.manifest = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload.unknown_files[name] = raw
                continue

            if name == "project.json":
                raw = zf.read(name)
                try:
                    payload.project_json = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload.unknown_files[name] = raw
                continue

            # Known module JSON files
            if name in KNOWN_JSON_FILES:
                raw = zf.read(name)
                try:
                    payload.module_payloads[name] = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload.unknown_files[name] = raw
                continue

            # Everything else (unknown JSON files, binary files, future modules)
            raw = zf.read(name)
            # Attempt JSON parse for .json files we don't recognize
            if name.endswith(".json"):
                try:
                    payload.module_payloads[name] = json.loads(raw)
                    continue
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            payload.unknown_files[name] = raw

        return payload

    @staticmethod
    def load_from_file(filepath: str, *, defer_assets: bool = True) -> ProjectArchivePayload:
        """
        Convenience: open a .topos file and return a payload.

        Args:
            filepath: Path to the .topos archive.
            defer_assets: When True (default), ``assets/`` members are
                preserved lazily (streamed at write time) instead of being
                fully loaded into memory (AUD-36901).

        Returns:
            An empty payload if the file does not exist (nothing to preserve).

        Raises:
            ProjectArchiveReadError: If *filepath* exists but cannot be
                opened or read. AUD-36902: a corrupt archive must not be
                silently swapped for an empty payload — saving with an
                empty payload would drop every other product's module data.
        """
        import os

        if not filepath or not os.path.exists(filepath):
            return ProjectArchivePayload()

        try:
            with zipfile.ZipFile(filepath, "r") as zf:
                return ProjectArchivePayload.read_from_zip(
                    zf, defer_assets=defer_assets, source_path=filepath
                )
        except (zipfile.BadZipFile, OSError) as exc:
            logger.error(
                "Existing .topos is unreadable; refusing to return an empty "
                "payload that would drop preserved module data on save: "
                "%s (%s)",
                filepath,
                exc,
            )
            raise ProjectArchiveReadError(
                f"Cannot read existing .topos archive {filepath!r}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def write_to_zip(self, zf: zipfile.ZipFile) -> None:
        """
        Write all cached entries into an open ZipFile (write mode).

        This writes back every preserved file exactly as it was read,
        except for entries that have been removed via ``remove()``.
        Lazily preserved ``assets/`` members are streamed from their source
        archive (never fully resident in memory) and written without
        re-compression.
        """
        # 1. manifest
        self._write_json(zf, "manifest.json", self.manifest)

        # 2. project.json
        self._write_json(zf, "project.json", self.project_json)

        # 3. module payloads
        for path, data in self.module_payloads.items():
            self._write_json(zf, path, data)

        # 4. unknown / binary files (eager bytes)
        for path, raw in self.unknown_files.items():
            if path in self.file_members:
                # DESK-01B：新资源文件覆盖旧未知条目，避免重复 ZIP 成员。
                continue
            zf.writestr(path, raw)

        # 5. lazily preserved large assets (AUD-36901)
        for path, member in self.preserved_members.items():
            if path in self.unknown_files:
                # Fresh bytes (e.g. a re-rendered cache asset) win over the
                # stale preserved member.
                continue
            if path in self.file_members:
                # DESK-01B：同上——新文件成员优先，不写重复条目。
                continue
            self._stream_preserved_member(zf, member)

        # 6. plain-file members (DESK-01B embedded resources) — fresh files
        # win over any stale preserved/unknown entry with the same name.
        for path, member in self.file_members.items():
            self._stream_file_member(zf, member)

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def set_manifest(self, data: dict[str, Any]) -> None:
        """Replace the manifest dict."""
        self.manifest = data
        self.unknown_files.pop("manifest.json", None)
        self.preserved_members.pop("manifest.json", None)

    def set_project_json(self, data: dict[str, Any]) -> None:
        """Replace the project.json dict."""
        self.project_json = data
        self.unknown_files.pop("project.json", None)
        self.preserved_members.pop("project.json", None)

    def set_module(self, path: str, data: dict[str, Any]) -> None:
        """
        Set (overwrite) a known module JSON file.

        If the path was previously stored in unknown_files, it is moved
        to module_payloads.
        """
        self.module_payloads[path] = data
        self.unknown_files.pop(path, None)
        self.preserved_members.pop(path, None)
        self.file_members.pop(path, None)

    def remove_module(self, path: str) -> None:
        """Remove a module from the payload entirely."""
        self.module_payloads.pop(path, None)
        self.unknown_files.pop(path, None)
        self.preserved_members.pop(path, None)
        self.file_members.pop(path, None)

    def add_file_member(self, path: str, source_path: str) -> None:
        """DESK-01B：登记一个本机文件，写包时流式拷入 *path*。

        同路径的既有条目（preserved/unknown）在写出时被本条目覆盖；
        源文件必须存在（校验留给调用方——保存前预检负责友好报错）。
        """
        import os as _os
        import time as _time

        stat = _os.stat(source_path)
        self.file_members[path] = _FileMember(
            source_path=source_path,
            filename=path,
            date_time=_time.localtime(stat.st_mtime)[:6],
            file_size=stat.st_size,
        )

    def get_module(self, path: str) -> dict[str, Any] | None:
        """Get a module payload by path, or None."""
        return self.module_payloads.get(path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _stream_preserved_member(
        self,
        zf: zipfile.ZipFile,
        member: _PreservedMember,
    ) -> None:
        """Stream one preserved asset from its source archive into *zf*.

        Raises:
            ProjectArchiveReadError: if the source archive disappeared or
                the member can no longer be read. Failing loudly keeps a
                save from silently dropping the asset.
        """
        try:
            source_zf = zipfile.ZipFile(member.source_path, "r")
        except (zipfile.BadZipFile, OSError) as exc:
            raise ProjectArchiveReadError(
                f"Preserved asset {member.filename!r} cannot be re-read from "
                f"{member.source_path!r}; refusing to drop it: {exc}"
            ) from exc
        try:
            with source_zf.open(member.filename) as src:
                info = zipfile.ZipInfo(member.filename, date_time=member.date_time)
                info.compress_type = PRESERVED_ASSET_COMPRESS_TYPE
                with zf.open(info, "w") as dst:
                    shutil.copyfileobj(src, dst, _STREAM_COPY_CHUNK)
        except (KeyError, zipfile.BadZipFile, OSError) as exc:
            raise ProjectArchiveReadError(
                f"Preserved asset {member.filename!r} could not be streamed "
                f"from {member.source_path!r}: {exc}"
            ) from exc
        finally:
            source_zf.close()

    def _stream_file_member(
        self,
        zf: zipfile.ZipFile,
        member: _FileMember,
    ) -> None:
        """Stream one plain filesystem file into *zf* (DESK-01B).

        Raises:
            ProjectArchiveReadError: 源文件消失或不可读——宁可保存失败也
                不静默丢资源。
        """

        info = zipfile.ZipInfo(member.filename, date_time=member.date_time)
        info.compress_type = PRESERVED_ASSET_COMPRESS_TYPE
        try:
            with open(member.source_path, "rb") as src, zf.open(info, "w") as dst:
                shutil.copyfileobj(src, dst, _STREAM_COPY_CHUNK)
        except OSError as exc:
            raise ProjectArchiveReadError(
                f"Resource file {member.filename!r} cannot be streamed from "
                f"{member.source_path!r}: {exc}"
            ) from exc

    @staticmethod
    def _write_json(
        zf: zipfile.ZipFile,
        path: str,
        data: dict[str, Any],
    ) -> None:
        """Serialize *data* as pretty-printed JSON into the zip."""
        raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        zf.writestr(path, raw)
