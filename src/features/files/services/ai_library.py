"""Persistent index of reusable media and timed analysis in the Files catalog."""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import re
import stat
import threading
import time
import unicodedata
import weakref
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from typing import Any, Literal

from src.features.files.repository.catalog import FilesCatalog, LibraryRecord, nfc
from src.features.files.services.ai_library_vectors import (
    VECTOR_EVIDENCE_INDEX,
    VECTOR_SCHEMA,
    EmbedTexts,
    EvidenceVectors,
    model_identity,
)
from src.features.files.services.query import classify
from src.features.files.transactions import CancelToken

_SCHEMA_KEY = "ai_library_schema"
_SCHEMA_VERSION = "3"
_POLICY_KEY = "ai_library_analysis_policy"
_MAX_QUERY_LENGTH = 1000
_MEDIA_KINDS = ("image", "video", "audio")
_WORDS = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+|[^\W_]+", re.UNICODE)
_HAN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_STOP_WORDS = frozenset({"a", "an", "and", "at", "for", "i", "in", "of", "on", "the", "to", "with"})
_BUILD_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_BUILD_LOCKS_GUARD = threading.Lock()

_SCHEMA = (
    """CREATE TABLE ai_library_assets (
        asset_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        analyzer_revision TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL
    )""",
    """CREATE TABLE ai_library_locations (
        path TEXT PRIMARY KEY,
        catalog_path TEXT NOT NULL,
        library_id TEXT NOT NULL,
        asset_id TEXT NOT NULL REFERENCES ai_library_assets(asset_id),
        name TEXT NOT NULL,
        file_state TEXT NOT NULL,
        online INTEGER NOT NULL DEFAULT 1
    )""",
    "CREATE INDEX ai_library_locations_asset ON ai_library_locations(asset_id, library_id)",
    """CREATE TABLE ai_library_evidence (
        id INTEGER PRIMARY KEY,
        asset_id TEXT NOT NULL REFERENCES ai_library_assets(asset_id),
        text TEXT NOT NULL,
        tags TEXT NOT NULL,
        tag_search TEXT NOT NULL,
        start_seconds REAL,
        end_seconds REAL,
        modality TEXT NOT NULL
    )""",
    "CREATE INDEX ai_library_evidence_asset ON ai_library_evidence(asset_id)",
    """CREATE VIRTUAL TABLE ai_library_evidence_fts USING fts5(
        text, tags, tokenize='trigram'
    )""",
    """CREATE TRIGGER ai_library_evidence_insert AFTER INSERT ON ai_library_evidence BEGIN
        INSERT INTO ai_library_evidence_fts(rowid, text, tags)
        VALUES (new.id, new.text, new.tag_search);
    END""",
    """CREATE TRIGGER ai_library_evidence_delete AFTER DELETE ON ai_library_evidence BEGIN
        DELETE FROM ai_library_evidence_fts WHERE rowid = old.id;
    END""",
)

_SNAPSHOT_SCHEMA = (
    "CREATE INDEX ai_library_locations_catalog_path ON ai_library_locations(catalog_path)",
    "CREATE INDEX ai_library_locations_library_path ON ai_library_locations(library_id, catalog_path)",
    """CREATE TABLE ai_library_analysis_snapshots (
        id INTEGER PRIMARY KEY,
        asset_id TEXT NOT NULL REFERENCES ai_library_assets(asset_id),
        cache_scope TEXT NOT NULL,
        stage TEXT NOT NULL,
        revision TEXT NOT NULL,
        completed INTEGER NOT NULL
    )""",
    """CREATE INDEX ai_library_snapshots_lookup
       ON ai_library_analysis_snapshots(asset_id, cache_scope, stage, revision, completed, id DESC)""",
    """CREATE TABLE ai_library_location_analyses (
        path TEXT NOT NULL REFERENCES ai_library_locations(path),
        stage TEXT NOT NULL,
        snapshot_id INTEGER NOT NULL REFERENCES ai_library_analysis_snapshots(id),
        PRIMARY KEY(path, stage)
    )""",
    "CREATE INDEX ai_library_location_snapshots ON ai_library_location_analyses(snapshot_id, path)",
    """ALTER TABLE ai_library_evidence ADD COLUMN snapshot_id INTEGER
       REFERENCES ai_library_analysis_snapshots(id)""",
    "CREATE INDEX ai_library_snapshot_evidence ON ai_library_evidence(snapshot_id)",
)


def _media_kinds(kinds: tuple[str, ...] | None) -> tuple[str, ...]:
    if kinds is None:
        return _MEDIA_KINDS
    if not isinstance(kinds, (tuple, list)) or any(kind not in _MEDIA_KINDS for kind in kinds):
        raise ValueError("素材类型无效")
    return tuple(kind for kind in _MEDIA_KINDS if kind in kinds)


@dataclass(frozen=True)
class MediaEvidence:
    """An analyzer's description, optionally linked to a source time range."""

    text: str
    start_seconds: float | None = None
    end_seconds: float | None = None
    tags: tuple[str, ...] = ()
    modality: str = "visual"


@dataclass(frozen=True)
class LibraryAnalysisPolicy:
    """Online inference is allowed only for explicitly approved bound routes."""

    mode: Literal["basic", "profiles"] = "basic"
    approved_routes: tuple[str, ...] = ()
    models: tuple[tuple[str, str], ...] | None = None
    kinds: tuple[str, ...] = _MEDIA_KINDS
    recursive: bool = True

    def __post_init__(self) -> None:
        if self.mode not in ("basic", "profiles") or (self.mode == "basic" and self.approved_routes):
            raise ValueError("素材分析模式或上传授权无效")
        if len(self.approved_routes) > 6 or any(
            not isinstance(route, str) or not re.fullmatch("[a-f0-9]{64}", route)
            for route in self.approved_routes
        ):
            raise ValueError("素材上传授权无效")
        if self.models is not None:
            if any(kind not in _MEDIA_KINDS or not isinstance(profile, str) or not profile.strip()
                   for kind, profile in self.models) or len(dict(self.models)) != len(self.models):
                raise ValueError("素材模型配置无效")
        if not isinstance(self.recursive, bool) or self.kinds is None:
            raise ValueError("素材扫描范围无效")
        object.__setattr__(self, "kinds", _media_kinds(self.kinds))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "approved_routes": list(self.approved_routes),
            "models": dict(self.models) if self.models is not None else None,
            "kinds": list(self.kinds), "recursive": self.recursive,
        }


_Analyzer = Callable[[str, str], Sequence[MediaEvidence]]


@dataclass(frozen=True)
class _AnalysisStage:
    analyze: _Analyzer
    revision: str
    scope: str
    modalities: tuple[str, ...]
    kinds: tuple[str, ...]

    @classmethod
    def of(cls, analyze: _Analyzer, revision: str | None = None) -> _AnalysisStage:
        return cls(analyze, str(revision if revision is not None else getattr(analyze, "revision", "1")),
                   getattr(analyze, "index_scope", "complete"), tuple(getattr(analyze, "index_modalities", ())),
                   tuple(getattr(analyze, "index_kinds", ())))


def _stage_revisions(value: str) -> dict[str, str]:
    if value and value.startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return {key: item for key, item in parsed.items() if isinstance(item, str)}
        return {}
    return {"complete": value} if value else {}


@dataclass(frozen=True)
class _MediaFile:
    library_id: str
    path: str
    kind: str


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _prefix(folder: Path) -> str:
    return nfc(folder).rstrip(os.sep) + os.sep


def _directory_identity(path: Path) -> str:
    """Validate each lexical ancestor without resolving a symlink."""
    for component in (*reversed(path.parents), path):
        try:
            value = component.lstat()
        except OSError:
            return ""
        if not stat.S_ISDIR(value.st_mode):
            raise ValueError("请选择真实文件夹，素材范围不能经过符号链接")
    return f"{path}:{value.st_dev}:{value.st_ino}"


@dataclass(frozen=True)
class _DirectoryScope:
    library: LibraryRecord
    folder: Path
    nested_roots: tuple[Path, ...]
    recursive: bool
    kinds: tuple[str, ...] = _MEDIA_KINDS

    @property
    def cache_key(self) -> str:
        return self.library.library_id + ":" + nfc(self.folder)

    def sql(self, kind_column: str) -> tuple[str, tuple[Any, ...]]:
        prefix = _prefix(self.folder)
        # Paths are stored with binary collation. The next separator byte
        # bounds all descendants without LIKE wildcard or sibling matches.
        upper = prefix[:-1] + chr(ord(os.sep) + 1)
        conditions = ["l.library_id = ?", "l.catalog_path >= ?", "l.catalog_path < ?"]
        params: list[Any] = [self.library.library_id, prefix, upper]
        if not self.recursive:
            conditions.append("instr(substr(l.catalog_path, ?), ?) = 0")
            params.extend((len(prefix) + 1, os.sep))
        for nested in self.nested_roots:
            child = _prefix(nested)
            conditions.append("substr(l.catalog_path, 1, ?) != ?")
            params.extend((len(child), child))
        conditions.append(f"{kind_column} IN ({','.join('?' for _ in self.kinds)})")
        params.extend(self.kinds)
        return " AND ".join(conditions), tuple(params)


def _directory_scope(
    library: LibraryRecord, libraries: Sequence[LibraryRecord],
    folder: str | None, recursive: bool,
) -> _DirectoryScope:
    root = Path(library.root)
    selected = Path(folder) if folder is not None else root
    normalized = Path(nfc(selected))
    if not selected.is_absolute() or ".." in selected.parts or not _under(normalized, root):
        raise ValueError("请选择该素材库中的绝对文件夹路径")
    nested = tuple(Path(record.root) for record in libraries
                   if record.library_id != library.library_id and _under(Path(record.root), root))
    if any(_under(normalized, child) for child in nested):
        raise ValueError("此文件夹属于另一个已登记的素材库")
    return _DirectoryScope(library, selected, tuple(child for child in nested if _under(child, normalized)), recursive)


def _scope_sql(scopes: Sequence[_DirectoryScope], kind_column: str) -> tuple[str, tuple[Any, ...]]:
    clauses = [scope.sql(kind_column) for scope in scopes]
    directory = " OR ".join(f"({sql})" for sql, _ in clauses)
    params = tuple(value for _, args in clauses for value in args)
    return f"({directory})", params


class _Cancelled(Exception):
    pass


_Cancel = CancelToken | threading.Event | None


def _cancelled(cancel: _Cancel) -> bool:
    if isinstance(cancel, threading.Event):
        return cancel.is_set()
    return cancel is not None and cancel.requested


def _file_state(path: str) -> str:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("素材必须是普通文件")
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def _asset_identity(path: str, kind: str, cancel: _Cancel) -> str:
    if _cancelled(cancel):
        raise _Cancelled()
    digest = hashlib.sha256(kind.encode("ascii") + b"\0")
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            if _cancelled(cancel):
                raise _Cancelled()
            digest.update(chunk)
    return f"media_{digest.hexdigest()}"


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _query_terms(query: str) -> tuple[str, ...]:
    terms: list[str] = []
    for match in _WORDS.finditer(query):
        word = match[0]
        if _HAN.fullmatch(word[:1]):
            han = _HAN.findall(word)
            if len(han) <= 2:
                terms.append("".join(han))
            else:
                terms.extend("".join(han[index:index + length])
                             for length in (3, 2) for index in range(len(han) - length + 1))
        elif word not in _STOP_WORDS:
            terms.append(word)
    return tuple(dict.fromkeys(terms))[:64]


def _match_score(query: str, terms: tuple[str, ...], text: str) -> float:
    text = _normalized_text(text)
    return (4.0 if query and query in text else 0.0) + sum(
        min(len(term), 6) / 3 for term in terms if term in text
    )


class AILibraryService:
    """Independent AI evidence over registered media folders and manual catalog metadata."""

    def __init__(self, catalog: FilesCatalog) -> None:
        self._catalog = catalog
        with _BUILD_LOCKS_GUARD:
            key = str(catalog.locator.catalog_db.resolve())
            self._build_lock = _BUILD_LOCKS.get(key) or threading.Lock()
            _BUILD_LOCKS[key] = self._build_lock
            vector_key = key + ":vectors"
            self._vector_lock = _BUILD_LOCKS.get(vector_key) or threading.Lock()
            _BUILD_LOCKS[vector_key] = self._vector_lock
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        def version(cursor: Any) -> str:
            row = cursor.execute(
                "SELECT value FROM settings WHERE library_id = '' AND key = ?", (_SCHEMA_KEY,)
            ).fetchone()
            return str(row[0]) if row else ""

        with self._catalog._read() as cursor:
            existing = version(cursor)
        if existing == _SCHEMA_VERSION:
            return
        with self._catalog._write() as cursor:
            existing = version(cursor)
            if existing == _SCHEMA_VERSION:
                return
            if existing not in ("", "1", "2"):
                raise RuntimeError(f"不支持的 AI 素材索引版本 {existing}")
            if not existing:
                for statement in _SCHEMA:
                    cursor.execute(statement)
            if existing != "2":
                for statement in _SNAPSHOT_SCHEMA:
                    cursor.execute(statement)
            if existing == "1":
                self._migrate_unique_legacy_evidence(cursor)
            cursor.execute(VECTOR_SCHEMA)
            cursor.execute(VECTOR_EVIDENCE_INDEX)
            cursor.execute(
                """INSERT INTO settings VALUES ('', ?, ?)
                   ON CONFLICT(library_id, key) DO UPDATE SET value = excluded.value""",
                (_SCHEMA_KEY, _SCHEMA_VERSION),
            )

    def _migrate_unique_legacy_evidence(self, cursor: Any) -> None:
        # V1's last writer was tracked per asset, never per location. Evidence
        # with multiple possible source locations cannot safely be assigned.
        roots = {record.library_id: record.root for record in self._catalog.libraries()}
        rows = cursor.execute(
            """SELECT source.path, source.library_id, source.asset_id, asset.analyzer_revision
               FROM ai_library_locations source
               JOIN ai_library_assets asset ON asset.asset_id = source.asset_id
               JOIN (SELECT asset_id FROM ai_library_locations GROUP BY asset_id HAVING COUNT(*) = 1) unique_source
                    ON unique_source.asset_id = source.asset_id"""
        ).fetchall()
        modality_stages = {"ocr": "basic", "visual": "vision", "transcript": "asr"}
        for row in rows:
            root = roots.get(row["library_id"])
            if root is None:
                continue
            revisions = _stage_revisions(row["analyzer_revision"])
            assigned: dict[str, list[int]] = {}
            for evidence in cursor.execute(
                "SELECT id, modality FROM ai_library_evidence WHERE asset_id = ?", (row["asset_id"],),
            ):
                stage = modality_stages.get(evidence["modality"], "complete")
                assigned.setdefault(stage if stage in revisions else "complete", []).append(evidence["id"])
            for stage in dict.fromkeys((*revisions, *assigned)):
                cursor.execute(
                    """INSERT INTO ai_library_analysis_snapshots(asset_id, cache_scope, stage, revision, completed)
                       VALUES (?, ?, ?, ?, ?)""",
                    (row["asset_id"], row["library_id"] + ":" + root, stage, revisions.get(stage, ""),
                     int(stage in revisions)),
                )
                snapshot = cursor.lastrowid
                cursor.executemany("UPDATE ai_library_evidence SET snapshot_id = ? WHERE id = ?",
                                   ((snapshot, entry) for entry in assigned.get(stage, ())))
                cursor.execute("INSERT INTO ai_library_location_analyses VALUES (?, ?, ?)",
                               (row["path"], stage, snapshot))

    @staticmethod
    def _policy_key(scope: _DirectoryScope) -> str:
        root = Path(scope.library.root)
        if Path(nfc(scope.folder)) == root:
            return _POLICY_KEY
        relative = str(Path(nfc(scope.folder)).relative_to(root))
        return f"{_POLICY_KEY}:{relative}"

    def analysis_scope_stamp(self, library_id: str, *, folder: str | None = None) -> tuple[str, str] | None:
        """Return current disk identities for binding a displayed upload authorization."""
        libraries = self._catalog.libraries()
        library = next((record for record in libraries if record.library_id == library_id), None)
        if library is None:
            return None
        scope = _directory_scope(library, libraries, folder, True)
        root, selected = _directory_identity(Path(library.root)), _directory_identity(scope.folder)
        return (root, selected) if root and selected else None

    def analysis_policy(self, library_id: str, *, folder: str | None = None) -> LibraryAnalysisPolicy:
        libraries = self._catalog.libraries()
        library = next((record for record in libraries if record.library_id == library_id), None)
        if library is None:
            return LibraryAnalysisPolicy()
        scope = _directory_scope(library, libraries, folder, True)
        selected_identity = _directory_identity(scope.folder) if folder is not None else ""
        key = self._policy_key(scope)
        with self._catalog._read() as cursor:
            row = cursor.execute(
                "SELECT value FROM settings WHERE library_id = ? AND key = ?", (library_id, key)
            ).fetchone()
        if row:
            try:
                data = json.loads(row[0])
                if isinstance(data, dict) and isinstance(data.get("approved_routes"), list):
                    models = data.get("models")
                    if models is not None and not isinstance(models, dict):
                        raise ValueError("素材模型配置无效")
                    kinds = data.get("kinds", _MEDIA_KINDS)
                    recursive = data.get("recursive", True)
                    current = False
                    if data["approved_routes"]:
                        source = selected_identity if folder is not None else _directory_identity(scope.folder)
                        current = bool(source) and source == data.get("source_directory")
                        if current and key != _POLICY_KEY:
                            root = _directory_identity(Path(library.root))
                            current = bool(root) and root == data.get("library_directory")
                    return LibraryAnalysisPolicy(
                        data["mode"], tuple(data["approved_routes"]) if current else (),
                        tuple(models.items()) if models is not None else None,
                        _media_kinds(kinds), recursive,
                    )
            except (KeyError, TypeError, ValueError):
                pass
        return LibraryAnalysisPolicy()

    def set_analysis_policy(
        self, library_id: str, policy: LibraryAnalysisPolicy, *, folder: str | None = None,
        expected_scope_stamp: tuple[str, str] | None = None,
    ) -> None:
        with self._catalog._write() as cursor:
            libraries = self._catalog.libraries()
            library = next((record for record in libraries if record.library_id == library_id), None)
            if library is None:
                raise ValueError("素材目录已取消登记")
            scope = _directory_scope(library, libraries, folder, True)
            key = self._policy_key(scope)
            identity = _directory_identity(scope.folder)
            if not identity and folder is not None:
                raise ValueError("请选择可访问的素材文件夹")
            payload = policy.to_dict()
            if policy.approved_routes:
                root = _directory_identity(Path(library.root))
                if not identity or not root:
                    raise ValueError("授权前请连接素材目录")
                if expected_scope_stamp is not None and expected_scope_stamp != (root, identity):
                    raise ValueError("素材文件夹已更换，请重新确认上传授权")
                payload["source_directory"] = identity
                if key != _POLICY_KEY:
                    payload["library_directory"] = root
            cursor.execute(
                """INSERT INTO settings (library_id, key, value) VALUES (?, ?, ?)
                   ON CONFLICT(library_id, key) DO UPDATE SET value = excluded.value""",
                (library_id, key, json.dumps(payload)),
            )

    def build_index(
        self,
        analyze: _Analyzer,
        *,
        cancel: _Cancel = None,
        progress: Callable[[int, int], None] | None = None,
        analyzer_revision: str | None = None,
        additional_analyzers: Callable[[str], Sequence[_Analyzer]] | None = None,
        library_id: str | None = None,
        folder: str | None = None,
        kinds: tuple[str, ...] | None = None,
        recursive: bool = True,
        force: bool = False,
        only_unanalyzed: bool = False,
        per_library_scope: dict[str, tuple[tuple[str, ...], bool]] | None = None,
    ) -> dict[str, Any]:
        """Analyze new or modified contents once; old analysis survives failed retries."""
        if force and only_unanalyzed:
            raise ValueError("重新分析不能和仅分析未处理素材同时使用")
        if not self._build_lock.acquire(blocking=False):
            raise RuntimeError("AI 素材库正在建立索引")
        try:
            basic = _AnalysisStage.of(analyze, analyzer_revision)
            scopes = self._scopes(library_id, folder, recursive)
            if not scopes and library_id is not None:
                raise ValueError("该素材库已移除，请重新选择。")
            selected_kinds = _media_kinds(kinds)
            restrictions = per_library_scope or {}
            scan_scopes = []
            for scope in scopes:
                owner_kinds, owner_recursive = restrictions.get(scope.library.library_id, (_MEDIA_KINDS, True))
                effective = tuple(kind for kind in _media_kinds(owner_kinds) if kind in selected_kinds)
                if effective:
                    scan_scopes.append(replace(scope, kinds=effective, recursive=scope.recursive and owner_recursive))
            return self._build(basic, cancel, progress, additional_analyzers, tuple(scan_scopes), force, only_unanalyzed)
        finally:
            self._build_lock.release()

    def _scopes(
        self, library_id: str | None, folder: str | None, recursive: bool,
    ) -> tuple[_DirectoryScope, ...]:
        if folder is not None and library_id is None:
            raise ValueError("请先选择素材文件夹所属的库")
        registered = self._catalog.libraries()
        return tuple(_directory_scope(library, registered, folder, recursive)
                     for library in registered if library_id is None or library.library_id == library_id)

    def _build(
        self, basic: _AnalysisStage, cancel: _Cancel, progress: Callable[[int, int], None] | None,
        additional_analyzers: Callable[[str], Sequence[_Analyzer]] | None,
        scopes: tuple[_DirectoryScope, ...], force: bool, only_unanalyzed: bool,
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "indexed": 0, "metadata_only": 0, "skipped": 0, "failed": 0, "offline": 0,
            "unavailable_libraries": 0, "cancelled": False, "errors": [], "warnings": [],
        }
        if not scopes:
            return report
        sources: list[_MediaFile] = []
        unavailable: set[str] = set()
        incomplete: set[str] = set()
        identities: dict[str, str] = {}
        scope_by_library = {scope.library.library_id: scope for scope in scopes}
        for scope in scopes:
            if _cancelled(cancel):
                report["cancelled"] = True
                return report
            library_id = scope.library.library_id
            identity = _directory_identity(scope.folder)
            if not identity:
                unavailable.add(library_id)
                continue
            identities[library_id] = identity
            sources.extend(self._walk_library(scope, incomplete, cancel))

        if progress:
            progress(0, len(sources))
        analyzed = (self._analyzed_paths([source.path for source in sources], self._catalog.libraries(), cancel)
                    if only_unanalyzed else set())
        stages: dict[str, tuple[_AnalysisStage, ...]] = {}
        forced_snapshots: dict[tuple[str, str, str, str], int] | None = {} if force else None
        for done, source in enumerate(sources, 1):
            try:
                if _cancelled(cancel):
                    raise _Cancelled()
                if source.path in analyzed:
                    report["skipped"] += 1
                    if progress:
                        progress(done, len(sources))
                    continue
                scope = scope_by_library[source.library_id]
                if not _directory_identity(Path(source.path).parent) or (
                    _directory_identity(scope.folder) != identities[source.library_id]
                ):
                    incomplete.add(source.library_id)
                    raise OSError("素材文件夹在扫描过程中已变更")
                if source.library_id not in stages:
                    extra = additional_analyzers(source.library_id) if additional_analyzers else ()
                    stages[source.library_id] = (basic, *map(_AnalysisStage.of, extra))
                results: set[str] = set()
                failures = []
                for stage in stages[source.library_id]:
                    if stage.kinds and source.kind not in stage.kinds:
                        continue
                    try:
                        results.add(self._index_file(source, stage, scope.cache_key, cancel, report, forced_snapshots))
                    except (OSError, RuntimeError, ValueError, TypeError, ImportError) as exc:
                        if _cancelled(cancel):
                            raise _Cancelled() from exc
                        failures.append(str(exc))
                if failures:
                    raise RuntimeError("；".join(dict.fromkeys(failures)))
                if "indexed" in results:
                    report["indexed"] += 1
                elif "metadata_only" in results:
                    report["metadata_only"] += 1
                else:
                    report["skipped"] += 1
            except _Cancelled:
                report["cancelled"] = True
                return report
            except (OSError, RuntimeError, ValueError, TypeError, ImportError) as exc:
                report["failed"] += 1
                if len(report["errors"]) < 10:
                    report["errors"].append({"path": source.path, "message": str(exc)})
            if progress:
                progress(done, len(sources))

        report["cancelled"] = _cancelled(cancel)
        if not report["cancelled"]:
            observed = {source.path for source in sources}
            for scope in scopes:
                library_id = scope.library.library_id
                if library_id not in unavailable:
                    try:
                        current = _directory_identity(scope.folder)
                    except ValueError:
                        current = ""
                    if current != identities[library_id]:
                        unavailable.add(library_id)
            sql, params = _scope_sql(scopes, "a.kind")
            with self._catalog._write() as cursor:
                scoped_rows = cursor.execute(
                    f"""SELECT l.path, l.library_id, l.online FROM ai_library_locations l
                        JOIN ai_library_assets a ON a.asset_id = l.asset_id WHERE {sql}""", params,
                ).fetchall()
                stale = [(row["path"],) for row in scoped_rows if row["online"]
                         and row["library_id"] not in unavailable | incomplete and row["path"] not in observed]
                cursor.executemany("UPDATE ai_library_locations SET online = 0 WHERE path = ?", stale)
                report["offline"] = len(stale) + sum(not row["online"] or row["library_id"] in unavailable
                                                      for row in scoped_rows)
            report["unavailable_libraries"] = len(unavailable)
        return report

    def _walk_library(
        self, scope: _DirectoryScope, incomplete: set[str], cancel: _Cancel,
    ) -> Iterable[_MediaFile]:
        nested_roots = {nfc(path) for path in scope.nested_roots}
        library_id = scope.library.library_id

        def walk_error(error: OSError) -> None:
            incomplete.add(library_id)

        for current, directories, files in os.walk(scope.folder, onerror=walk_error):
            if _cancelled(cancel):
                return
            directories[:] = sorted(
                name for name in directories
                if scope.recursive and not name.startswith(".")
                and nfc(os.path.join(current, name)) not in nested_roots
            )
            for name in sorted(files):
                if _cancelled(cancel):
                    return
                kind = classify(name)
                if name.startswith(".") or kind not in scope.kinds or name.lower().endswith(".topos"):
                    continue
                path = os.path.join(current, name)
                if not os.path.islink(path):
                    yield _MediaFile(library_id, path, kind)

    def _index_file(
        self, source: _MediaFile, analysis: _AnalysisStage, cache_scope: str,
        cancel: _Cancel, report: dict[str, Any],
        forced_snapshots: dict[tuple[str, str, str, str], int] | None,
    ) -> Literal["indexed", "metadata_only", "skipped"]:
        if _cancelled(cancel):
            raise _Cancelled()
        analyze, revision, stage = analysis.analyze, analysis.revision, analysis.scope
        state = _file_state(source.path)
        with self._catalog._read() as cursor:
            known = cursor.execute(
                """SELECT l.asset_id, l.library_id, l.online, s.revision, s.completed
                   FROM ai_library_locations l
                   JOIN ai_library_assets a ON a.asset_id = l.asset_id
                   LEFT JOIN ai_library_location_analyses b ON b.path = l.path AND b.stage = ?
                   LEFT JOIN ai_library_analysis_snapshots s ON s.id = b.snapshot_id
                   WHERE l.path = ? AND l.file_state = ? AND a.kind = ?""",
                (stage, source.path, state, source.kind),
            ).fetchone()
        if (forced_snapshots is None and known is not None and known["online"]
                and known["library_id"] == source.library_id and known["completed"]
                and known["revision"] == revision):
            return "skipped"
        asset_id = known["asset_id"] if known is not None else _asset_identity(source.path, source.kind, cancel)
        cache_key = (asset_id, cache_scope, stage, revision)
        if _file_state(source.path) != state:
            raise OSError("素材在读取指纹时已变更")
        with self._catalog._write() as cursor:
            # A file replaced by different bytes must never keep the prior
            # path's model output. A newly registered owner is also distinct.
            cursor.execute(
                """DELETE FROM ai_library_location_analyses WHERE path IN (
                       SELECT path FROM ai_library_locations
                       WHERE path = ? AND (asset_id != ? OR library_id != ?)
                   )""",
                (source.path, asset_id, source.library_id),
            )
            cursor.execute(
                "INSERT OR IGNORE INTO ai_library_assets(asset_id, kind, updated_at) VALUES (?, ?, ?)",
                (asset_id, source.kind, time.time()),
            )
            cursor.execute(
                """INSERT INTO ai_library_locations VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(path) DO UPDATE SET library_id=excluded.library_id,
                       asset_id=excluded.asset_id, file_state=excluded.file_state, online=1""",
                (source.path, nfc(source.path), source.library_id, asset_id, Path(source.path).name, state),
            )
            if forced_snapshots is None:
                cached = cursor.execute(
                    """SELECT id FROM ai_library_analysis_snapshots WHERE asset_id = ? AND cache_scope = ?
                       AND stage = ? AND revision = ? AND completed = 1 ORDER BY id DESC LIMIT 1""",
                    cache_key,
                ).fetchone()
                snapshot = cached[0] if cached else None
            else:
                snapshot = forced_snapshots.get(cache_key)
            if snapshot is not None:
                self._bind_snapshot(cursor, source.path, stage, snapshot)
                return "skipped"
        if _cancelled(cancel):
            raise _Cancelled()
        try:
            evidence = tuple(self._evidence_row(asset_id, item) for item in analyze(source.path, source.kind))
            if analysis.modalities and any(row[-1] not in analysis.modalities for row in evidence):
                raise ValueError("素材分析结果不符合当前模型的分析类型")
        finally:
            warnings = self._collect_warnings(analyze, source.path, report)
        if _cancelled(cancel):
            raise _Cancelled()
        if _file_state(source.path) != state:
            raise OSError("素材在分析过程中已变更")
        retryable = getattr(analyze, "retryable_failure", None)
        with self._catalog._write() as cursor:
            has_previous = cursor.execute(
                """SELECT 1
                   FROM ai_library_location_analyses b JOIN ai_library_evidence e ON e.snapshot_id = b.snapshot_id
                   WHERE b.path = ? AND b.stage = ? LIMIT 1""",
                (source.path, stage),
            ).fetchone() is not None
            failed = retryable is True or (
                retryable is None and (bool(warnings) or (has_previous and not evidence))
            )
            # Missing models can still provide a usable metadata index. An
            # empty warning-only result does not replace prior media evidence.
            keep_previous = has_previous and (failed or (warnings and not evidence))
            if not keep_previous and (not failed or evidence):
                cursor.execute(
                    """INSERT INTO ai_library_analysis_snapshots(asset_id, cache_scope, stage, revision, completed)
                       VALUES (?, ?, ?, ?, ?)""",
                    (*cache_key, int(not failed)),
                )
                snapshot = cursor.lastrowid
                cursor.executemany(
                    """INSERT INTO ai_library_evidence(asset_id, text, tags, tag_search, start_seconds,
                                                       end_seconds, modality, snapshot_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    ((*row, snapshot) for row in evidence),
                )
                self._bind_snapshot(cursor, source.path, stage, snapshot)
                if forced_snapshots is not None and not failed:
                    forced_snapshots[cache_key] = snapshot
                cursor.execute(
                    "UPDATE ai_library_assets SET updated_at = ? WHERE asset_id = ?", (time.time(), asset_id),
                )
        if failed:
            raise RuntimeError(warnings[0] if warnings else "素材分析未完成，可重新扫描")
        return "metadata_only" if not evidence and warnings else "indexed"

    @staticmethod
    def _bind_snapshot(cursor: Any, path: str, stage: str, snapshot: int) -> None:
        obsolete = cursor.execute(
            """SELECT old.id FROM ai_library_location_analyses b
               JOIN ai_library_analysis_snapshots old ON old.id = b.snapshot_id
               JOIN ai_library_analysis_snapshots new ON new.id = ?
               WHERE b.path = ? AND b.stage = ? AND old.id != new.id
                 AND (old.completed = 0 OR (old.revision = new.revision AND old.cache_scope = new.cache_scope))""",
            (snapshot, path, stage),
        ).fetchone()
        cursor.execute(
            """INSERT INTO ai_library_location_analyses VALUES (?, ?, ?)
               ON CONFLICT(path, stage) DO UPDATE SET snapshot_id = excluded.snapshot_id""",
            (path, stage, snapshot),
        )
        if obsolete and not cursor.execute(
            "SELECT 1 FROM ai_library_location_analyses WHERE snapshot_id = ? LIMIT 1", (obsolete[0],),
        ).fetchone():
            cursor.execute("DELETE FROM ai_library_evidence WHERE snapshot_id = ?", (obsolete[0],))
            cursor.execute("DELETE FROM ai_library_analysis_snapshots WHERE id = ?", (obsolete[0],))

    @staticmethod
    def _collect_warnings(analyze: Callable[..., Any], path: str, report: dict[str, Any]) -> tuple[str, ...]:
        raw = getattr(analyze, "warnings", ())
        if not isinstance(raw, (tuple, list)):
            return ()
        warnings = tuple(dict.fromkeys(text for message in raw if (text := str(message).strip())))
        existing = {item["message"] for item in report["warnings"]}
        distinct = [message for message in warnings if message not in existing]
        remaining = 10 - len(report["warnings"])
        report["warnings"].extend({"path": path, "message": message} for message in distinct[:remaining])
        return warnings

    @staticmethod
    def _evidence_row(asset_id: str, item: MediaEvidence) -> tuple[Any, ...]:
        if not isinstance(item, MediaEvidence):
            raise ValueError("素材分析结果格式无效")
        start, end = item.start_seconds, item.end_seconds
        if not (
            start is None and end is None
            or start is not None and end is not None and math.isfinite(start) and math.isfinite(end)
            and 0 <= start < end
        ):
            raise ValueError("素材分析结果包含无效的时间范围")
        tags = tuple(dict.fromkeys(tag.strip() for tag in item.tags if tag.strip()))
        return (asset_id, item.text.strip(), json.dumps(tags, ensure_ascii=False),
                " ".join(tags), start, end, item.modality)

    @staticmethod
    def _matches_file(path: str, expected: str) -> bool:
        try:
            return bool(_directory_identity(Path(path).parent)) and _file_state(path) == expected
        except (OSError, ValueError):
            return False

    def _matching_scopes(
        self, library_id: str | None, folder: str | None,
        kinds: tuple[str, ...] | None, recursive: bool,
    ) -> tuple[_DirectoryScope, ...]:
        scopes = self._scopes(library_id, folder, recursive)
        media_kinds = _media_kinds(kinds)
        if not scopes or not media_kinds:
            return ()
        if folder is not None:
            _directory_identity(scopes[0].folder)
        return tuple(replace(scope, kinds=media_kinds) for scope in scopes)

    def embedding_status(
        self, model_key: str, *, library_id: str | None = None, folder: str | None = None,
        kinds: tuple[str, ...] | None = None, recursive: bool = True, cancel: _Cancel = None,
    ) -> dict[str, int]:
        """Report coverage of completed evidence in the current catalog scope."""
        model_identity(model_key)
        if not _cancelled(cancel) and (scopes := self._matching_scopes(library_id, folder, kinds, recursive)):
            sql, params = _scope_sql(scopes, "i.kind")
            eligible, embedded, _ = EvidenceVectors(self._catalog, sql, params, model_key).status()
            if not _cancelled(cancel):
                return {"eligible": eligible, "embedded": embedded, "pending": eligible - embedded}
        return {"eligible": 0, "embedded": 0, "pending": 0}

    def build_embeddings(
        self, embed: EmbedTexts, *, model_key: str, library_id: str | None = None,
        folder: str | None = None, kinds: tuple[str, ...] | None = None, recursive: bool = True,
        cancel: _Cancel = None, progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Backfill missing vectors from validated text evidence, one committed batch at a time."""
        model_identity(model_key)
        if not self._vector_lock.acquire(blocking=False):
            raise RuntimeError("正在生成素材语义索引")
        try:
            scopes = self._matching_scopes(library_id, folder, kinds, recursive)
            return self._backfill_embeddings(embed, model_key, scopes, cancel, progress)
        finally:
            self._vector_lock.release()

    def _backfill_embeddings(
        self, embed: EmbedTexts, model_key: str, scopes: tuple[_DirectoryScope, ...],
        cancel: _Cancel, progress: Callable[[int, int], None] | None,
    ) -> dict[str, Any]:
        def cancelled() -> bool:
            return _cancelled(cancel)

        report: dict[str, Any] = {"eligible": 0, "embedded": 0, "skipped": 0, "pending": 0, "cancelled": cancelled()}
        if not scopes or cancelled():
            return report
        sql, params = _scope_sql(scopes, "i.kind")
        store = EvidenceVectors(self._catalog, sql, params, model_key)
        eligible, existing, last = store.status()
        pending = eligible - existing
        report.update(eligible=eligible, skipped=existing, pending=pending)
        if progress is not None:
            progress(0, pending)
        after = done = 0
        while not cancelled() and (rows := store.pending_after(after, last)):
            after = rows[-1]["id"]
            locations = self._verified_locations(
                {row["asset_id"] for row in rows}, {row["snapshot_id"] for row in rows},
                scopes, sql, params, {}, cancel,
            )
            ready = [row for row in rows if locations.get((row["asset_id"], row["snapshot_id"]), {}).get("online")]
            try:
                stored = store.embed_batch(ready, embed, cancelled=cancelled)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                if not cancelled():
                    report["error"] = str(error)
                break
            if cancelled():
                break
            report["embedded"] += stored
            report["skipped"] += len(rows) - stored
            done += len(rows)
            if progress is not None:
                progress(min(done, pending), pending)
        eligible, completed, _ = store.status()
        report.update(eligible=eligible, pending=eligible - completed, cancelled=cancelled())
        return report

    def analysis_status(self, paths: Sequence[str], *, cancel: _Cancel = None) -> set[str]:
        """Return paths with current, completed visual, audio or transcript evidence."""
        registered = self._catalog.libraries()
        return self._analyzed_paths(paths, registered, cancel)

    def _analyzed_paths(
        self, paths: Sequence[str], registered: Sequence[LibraryRecord], cancel: _Cancel,
    ) -> set[str]:
        matched: set[str] = set()
        candidates = iter(dict.fromkeys(path for path in paths if path and Path(path).is_absolute()))
        roots = sorted(((Path(record.root), record.library_id) for record in registered),
                       key=lambda root: len(root[0].parts), reverse=True)
        while batch := tuple(islice(candidates, 128)):
            if _cancelled(cancel):
                return set()
            placeholders = ",".join("?" for _ in batch)
            requested: dict[str, list[str]] = {}
            for path in batch:
                requested.setdefault(nfc(path), []).append(path)
            with self._catalog._read() as cursor:
                rows = cursor.execute(
                    f"""SELECT l.catalog_path, l.library_id, l.file_state FROM ai_library_locations l
                        WHERE l.catalog_path IN ({placeholders}) AND EXISTS (
                            SELECT 1 FROM ai_library_location_analyses b
                            JOIN ai_library_analysis_snapshots s ON s.id = b.snapshot_id
                            JOIN ai_library_evidence e ON e.snapshot_id = s.id
                            WHERE b.path = l.path AND s.completed = 1
                              AND e.modality IN ('visual', 'audio', 'transcript')
                              AND (e.text != '' OR e.tag_search != '')
                        )""",
                    tuple(nfc(path) for path in batch),
                ).fetchall()
            for row in rows:
                if _cancelled(cancel):
                    return set()
                catalog_path = row["catalog_path"]
                owner = next((library_id for root, library_id in roots if _under(Path(catalog_path), root)), None)
                if owner == row["library_id"]:
                    matched.update(path for path in requested[catalog_path]
                                   if path not in matched and self._matches_file(path, row["file_state"]))
        return matched

    def _unanalyzed(
        self, scopes: tuple[_DirectoryScope, ...],
        query: str, terms: tuple[str, ...], limit: int, cancel: _Cancel,
    ) -> list[dict[str, Any]]:
        registered = self._catalog.libraries()

        def candidates() -> Iterable[dict[str, Any]]:
            for scope in scopes:
                if _cancelled(cancel):
                    return
                try:
                    if not _directory_identity(scope.folder):
                        continue
                except ValueError:
                    continue
                files = iter(self._walk_library(scope, set(), cancel))
                while (batch := tuple(islice(files, 128))) and not _cancelled(cancel):
                    analyzed = self._analyzed_paths([source.path for source in batch], registered, cancel)
                    yield from self._pending_candidates(scope, batch, analyzed, query, terms, cancel)

        if query:
            hits = heapq.nsmallest(
                limit, candidates(), key=lambda hit: (-hit["score"], hit["name"].casefold(), hit["path"]),
            )
        else:
            hits = list(islice(candidates(), limit))
        return [] if _cancelled(cancel) else hits

    def _pending_candidates(
        self, scope: _DirectoryScope, files: tuple[_MediaFile, ...], analyzed: set[str],
        query: str, terms: tuple[str, ...], cancel: _Cancel,
    ) -> Iterable[dict[str, Any]]:
        paths = tuple(nfc(source.path) for source in files if source.path not in analyzed)
        if not paths:
            return
        placeholders = ",".join("?" for _ in paths)
        with self._catalog._read() as cursor:
            metadata = {
                row["path"]: row for row in cursor.execute(
                    f"""SELECT a.path, a.comment,
                               COALESCE((SELECT GROUP_CONCAT(tags.name, char(31)) FROM asset_tags t
                                         JOIN tags ON tags.id = t.tag_id
                                         WHERE t.library_id = a.library_id AND t.path = a.path), '') AS tags
                        FROM assets a WHERE a.library_id = ? AND a.path IN ({placeholders})""",
                    (scope.library.library_id, *paths),
                )
            }
            known = {
                row["path"]: (row["asset_id"], row["file_state"]) for row in cursor.execute(
                    f"""SELECT path, asset_id, file_state FROM ai_library_locations
                        WHERE library_id = ? AND catalog_path IN ({placeholders})""",
                    (scope.library.library_id, *paths),
                )
            }
        for source in files:
            if _cancelled(cancel):
                return
            path = source.path
            if path in analyzed:
                continue
            name = Path(path).name
            record = metadata.get(nfc(path))
            note, tags = (record["comment"], record["tags"]) if record else ("", "")
            score = _match_score(query, terms, " ".join((name, note, tags))) if query else 0.0
            if query and not score:
                continue
            try:
                if not _directory_identity(Path(path).parent):
                    continue
                state = _file_state(path)
            except (OSError, ValueError):
                continue
            asset_id, old_state = known.get(path, ("", ""))
            _, _, size, mtime_ns, ctime_ns = state.split(":")
            yield {
                "asset_id": asset_id if old_state == state else "", "path": path, "name": name,
                "kind": source.kind, "online": True, "source_size": int(size),
                "source_mtime_ns": int(mtime_ns), "source_ctime_ns": int(ctime_ns),
                "library_id": scope.library.library_id, "library_root": scope.library.root,
                "start_seconds": None, "end_seconds": None, "text": note, "tags": tags.split("\x1f") if tags else [],
                "modality": "metadata", "score": score,
            }

    def search(
        self, query: str, limit: int = 40, *, library_id: str | None = None,
        folder: str | None = None, kinds: tuple[str, ...] | None = None, recursive: bool = True,
        only_unanalyzed: bool = False, cancel: _Cancel = None,
        embedding: tuple[str, Sequence[float]] | None = None,
        semantic_library_ids: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search saved evidence and manual metadata; return original paths and source seconds."""
        limit = max(1, min(100, int(limit)))
        query = _normalized_text(query[:_MAX_QUERY_LENGTH])
        terms = _query_terms(query)
        if _cancelled(cancel):
            return []
        scopes = self._matching_scopes(library_id, folder, kinds, recursive)
        if not scopes:
            return []
        if only_unanalyzed:
            return self._unanalyzed(scopes, query, terms, limit, cancel)
        sql, params = _scope_sql(scopes, "i.kind")
        metadata = self._metadata_candidates(sql, params, query, terms, limit)
        evidence = self._evidence_candidates(sql, params, query, terms, limit)
        assets: dict[str, Any] = {}
        for row in metadata:
            asset_id = row["asset_id"]
            prior = assets.get(asset_id)
            if prior is None or _match_score(query, terms, row["search_text"]) > _match_score(
                query, terms, prior["search_text"]
            ):
                assets[asset_id] = row
        asset_ids = set(assets) | {row["asset_id"] for row in evidence}
        snapshot_ids = {row["snapshot_id"] for row in evidence}
        locations = self._verified_locations(
            asset_ids, snapshot_ids, scopes, sql, params,
            {asset_id: row["path"] for asset_id, row in assets.items()}, cancel,
        )
        lexical: list[tuple[tuple[str, int | str], dict[str, Any]]] = []
        matched_evidence: set[str] = set()
        for row in evidence:
            asset_id = row["asset_id"]
            location = locations.get((asset_id, row["snapshot_id"]))
            if location is None:
                continue
            score = _match_score(query, terms, f"{row['text']} {row['tag_search']}")
            if score <= 0:
                continue
            matched_evidence.add(asset_id)
            lexical.append((("evidence", row["id"]), self._evidence_hit(row, location, score + 1.0)))
        for asset_id, row in assets.items():
            location = locations.get((asset_id, None))
            if location is None or asset_id in matched_evidence:
                continue
            lexical.append((("metadata", asset_id), {
                **location,
                "start_seconds": None, "end_seconds": None, "text": row["comment"],
                "tags": row["tags"].split("\x1f") if row["tags"] else [], "modality": "metadata",
                "score": _match_score(query, terms, row["search_text"]),
            }))
        lexical.sort(key=lambda pair: self._hit_sort_key(pair[1]))
        if embedding is not None and query and not _cancelled(cancel):
            semantic_scopes = tuple(scope for scope in scopes if semantic_library_ids is None
                                    or scope.library.library_id in semantic_library_ids)
            if semantic_scopes:
                semantic = self._semantic_hits(semantic_scopes, embedding, limit, cancel)
                if semantic:
                    return [] if _cancelled(cancel) else self._fuse_ranks(lexical, semantic)[:limit]
        return [] if _cancelled(cancel) else [hit for _, hit in lexical[:limit]]

    @staticmethod
    def _evidence_hit(row: Any, location: dict[str, Any], score: float) -> dict[str, Any]:
        return {
            **location, "start_seconds": row["start_seconds"], "end_seconds": row["end_seconds"],
            "text": row["text"], "tags": json.loads(row["tags"]), "modality": row["modality"],
            "score": score,
        }

    @staticmethod
    def _hit_sort_key(hit: dict[str, Any]) -> tuple[bool, float, str, float]:
        return not hit["online"], -hit["score"], hit["name"].casefold(), hit["start_seconds"] or 0.0

    def _semantic_hits(
        self, scopes: tuple[_DirectoryScope, ...], embedding: tuple[str, Sequence[float]],
        limit: int, cancel: _Cancel,
    ) -> list[tuple[tuple[str, int | str], dict[str, Any]]]:
        sql, params = _scope_sql(scopes, "i.kind")
        model_key, query_vector = embedding

        def current_locations(rows: Sequence[Any]) -> dict[tuple[str, int | None], dict[str, Any]]:
            return self._verified_locations(
                {row["asset_id"] for row in rows}, {row["snapshot_id"] for row in rows},
                scopes, sql, params, {}, cancel,
            )

        def live_evidence(rows: Sequence[Any]) -> set[int]:
            locations = current_locations(rows)
            return {row["id"] for row in rows
                    if locations.get((row["asset_id"], row["snapshot_id"]), {}).get("online")}

        try:
            rows = EvidenceVectors(self._catalog, sql, params, model_key).nearest(
                query_vector, limit * 5, cancelled=lambda: _cancelled(cancel), live_evidence=live_evidence,
            )
        except (TypeError, ValueError, OverflowError):
            return []
        locations = current_locations(rows)
        hits = []
        for row in rows:
            location = locations.get((row["asset_id"], row["snapshot_id"]))
            if location is not None and location["online"]:
                hits.append((("evidence", row["id"]), self._evidence_hit(row, location, 0.0)))
        return hits

    @staticmethod
    def _fuse_ranks(
        lexical: list[tuple[tuple[str, int | str], dict[str, Any]]],
        semantic: list[tuple[tuple[str, int | str], dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        combined: dict[tuple[str, int | str], dict[str, Any]] = {}
        for ranking in (lexical, semantic):
            for rank, (key, hit) in enumerate(ranking, 1):
                first_score = combined[key]["score"] if key in combined else 0.0
                combined[key] = {**hit, "score": first_score + 1.0 / (60 + rank)}
        # Dictionaries retain insertion order, so exact rank ties favor the
        # lexical lane. A fused hit keeps the authorized semantic location.
        return sorted(combined.values(), key=lambda hit: (not hit["online"], -hit["score"]))

    def _metadata_candidates(
        self, scope_sql: str, params: tuple[Any, ...], query: str, terms: tuple[str, ...], limit: int,
    ) -> list[Any]:
        lookup = terms or ((query,) if query else ())
        conditions = " OR ".join("instr(lower(search_text), ?) > 0" for _ in lookup) or "1"
        with self._catalog._read() as cursor:
            return cursor.execute(
                f"""WITH details AS (
                    SELECT l.asset_id, l.path, l.name, l.online, i.updated_at, COALESCE(a.comment, '') AS comment,
                           COALESCE((SELECT GROUP_CONCAT(tags.name, char(31)) FROM asset_tags t
                                     JOIN tags ON tags.id = t.tag_id
                                     WHERE t.library_id = l.library_id AND t.path = l.catalog_path), '') AS tags
                    FROM ai_library_locations l JOIN ai_library_assets i ON i.asset_id = l.asset_id
                    LEFT JOIN assets a ON a.library_id = l.library_id AND a.path = l.catalog_path
                    WHERE {scope_sql}
                ), metadata AS (
                    SELECT *, name || ' ' || comment || ' ' || tags AS search_text FROM details
                )
                SELECT * FROM metadata WHERE {conditions}
                ORDER BY online DESC, updated_at DESC LIMIT ?""",
                (*params, *lookup, limit * 5),
            ).fetchall()

    def _evidence_candidates(
        self, scope_sql: str, params: tuple[Any, ...], query: str, terms: tuple[str, ...], limit: int,
    ) -> list[Any]:
        if not query:
            return []
        scope = f"""AND EXISTS (
            SELECT 1 FROM ai_library_location_analyses b
            JOIN ai_library_locations l ON l.path = b.path
            JOIN ai_library_assets i ON i.asset_id = l.asset_id
            WHERE b.snapshot_id = e.snapshot_id AND {scope_sql}
        )"""
        long_terms = list(dict.fromkeys(term for term in terms if len(term) >= 3))[:40]
        rows: list[Any] = []
        with self._catalog._read() as cursor:
            if long_terms:
                phrase = " OR ".join('"' + term.replace('"', '""') + '"' for term in long_terms)
                rows = cursor.execute(
                    f"""SELECT e.* FROM ai_library_evidence_fts JOIN ai_library_evidence e
                        ON e.id = ai_library_evidence_fts.rowid WHERE ai_library_evidence_fts MATCH ?
                        {scope} ORDER BY bm25(ai_library_evidence_fts) LIMIT ?""",
                    (phrase, *params, limit * 5),
                ).fetchall()
            # Recover literal short cues when trigram matching yields few
            # candidates, e.g. a request mentioning only «门铃» or «晨光».
            short_terms = tuple(term for term in terms if len(term) == 2)[:16]
            lookup = short_terms if long_terms else terms or (query,)
            if lookup and len(rows) < limit:
                conditions = " OR ".join(
                    "instr(lower(e.text || ' ' || e.tag_search), ?) > 0" for _ in lookup
                )
                fallback = cursor.execute(
                    f"""SELECT e.* FROM ai_library_evidence e
                        WHERE ({conditions}) {scope} ORDER BY e.id DESC LIMIT ?""",
                    (*lookup, *params, limit * 2),
                )
                seen = {row["id"] for row in rows}
                rows.extend(row for row in fallback if row["id"] not in seen)
        return rows

    def _verified_locations(
        self, asset_ids: set[str], snapshot_ids: set[int], scopes: tuple[_DirectoryScope, ...],
        scope_sql: str, params: tuple[Any, ...], preferred_paths: dict[str, str], cancel: _Cancel,
    ) -> dict[tuple[str, int | None], dict[str, Any]]:
        if not asset_ids:
            return {}
        ids = ",".join("?" for _ in asset_ids)
        roots = {scope.library.library_id: scope.library.root for scope in scopes}
        with self._catalog._read() as cursor:
            rows = cursor.execute(
                f"""SELECT l.*, i.kind, b.snapshot_id FROM ai_library_locations l
                    JOIN ai_library_assets i ON i.asset_id = l.asset_id
                    LEFT JOIN ai_library_location_analyses b ON b.path = l.path
                    WHERE l.asset_id IN ({ids}) AND {scope_sql}
                    ORDER BY l.online DESC, l.path""",
                (*asset_ids, *params),
            ).fetchall()
        rows.sort(key=lambda row: (row["path"] != preferred_paths.get(row["asset_id"]), not row["online"]))
        resolved: dict[tuple[str, int | None], dict[str, Any]] = {}
        files: dict[str, bool] = {}
        for row in rows:
            if _cancelled(cancel):
                return {}
            asset_id, path, snapshot = row["asset_id"], row["path"], row["snapshot_id"]
            keys = [(asset_id, None)]
            if snapshot in snapshot_ids:
                keys.append((asset_id, snapshot))
            keys = [key for key in keys if key not in resolved or not resolved[key]["online"]]
            if not keys:
                continue
            if path not in files:
                files[path] = self._matches_file(path, row["file_state"])
            online = files[path]
            for key in keys:
                if not online and key in resolved:
                    continue
                _, _, size, mtime_ns, ctime_ns = row["file_state"].split(":")
                resolved[key] = {
                    "asset_id": asset_id, "path": path, "name": row["name"],
                    "kind": row["kind"], "online": online, "source_size": int(size),
                    "source_mtime_ns": int(mtime_ns), "source_ctime_ns": int(ctime_ns),
                    "library_id": row["library_id"], "library_root": roots[row["library_id"]],
                }
        return resolved
