"""R02: LUT 资源注册、导入、迁移与重定位（无 Qt，headless 可测）。

权威与身份规则（docs/color_resources_backend_implementation_plan.md R02）：

- **验证**：``.cube``/``.3dl``/``.look`` 按实际解析能力验证（维度、数据
  长度、DOMAIN、非有限值、尺寸上限）；未支持/损坏格式给结构化错误码，
  不因文件对话框允许选择就宣称支持。
- **注册**：内容 SHA-256 + 资源 ID（``lut_<sha32>``，与机器路径无关）；
  同内容多路径去重为单条目，同名不同内容自然共存（archive_path 按
  内容散列分路径）。复用 ``resource_resolver.ResourceEntry``/``ResourceIndex``
  的既有资源容器与 ``topos-res://`` 引用。
- **导入**：验证通过后先原子拷贝进会话暂存目录（临时文件 + ``os.replace``），
  失败（磁盘满/中断/权限）不产生可应用条目；保存时经
  ``save_payload()`` 并入 ``resources_data``/``resource_files`` 走工程
  包既有原子写入（DESK-03D 取消/失败不留半包）。
- **迁移/重定位**：旧 ``active_lut_path`` 绝对路径可经 ``migrate_legacy``
  注册为工程资源引用（旧载荷字段保留不删）；文件缺失给 ``missing``
  状态与重定位路径，绝不静默移除。
- 生命周期：进程级单例会话（``get_lut_resource_session``）——工程加载时
  ``bind_project`` 合并包内索引，换工程/关闭时 ``reset_session`` 清理暂存。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from src.shared.color.lut_loader import load_lut
from src.shared.project.resource_resolver import (
    ResourceEntry,
    ResourceIndex,
    sha256_file,
)

#: 文件对话框允许选择的扩展名与实际验证解耦：能否支持由解析结果判定。
SUPPORTED_EXTENSIONS = (".cube", ".3dl", ".look")

#: 尺寸护栏：256³ × 3 × float32 ≈ 192MB 解析峰值即拒绝（parse 前判定）。
MAX_LUT_GRID_SIZE = 256
MAX_LUT_FILE_BYTES = 256 << 20  # 256 MB 文本上限


class LutResourceErrorCode(str, Enum):
    """机器可读错误码（UI 显示走 i18n，不拼错误字符串）。"""

    UNSUPPORTED_FORMAT = "unsupported_format"
    READ_FAILED = "read_failed"
    PARSE_FAILED = "parse_failed"
    NON_FINITE = "non_finite_values"
    INVALID_DOMAIN = "invalid_domain"
    TOO_LARGE = "too_large"
    WRITE_FAILED = "write_failed"
    CANCELLED = "cancelled"
    MISSING = "missing"
    HASH_MISMATCH = "hash_mismatch"


class LutResourceError(Exception):
    """结构化导入/解析失败（code 供 UI 诊断，不抛裸异常字符串）。"""

    def __init__(self, code: LutResourceErrorCode, message: str,
                 path: str = "") -> None:
        super().__init__(f"{code.value}: {message} ({path})")
        self.code = code
        self.message = message
        self.path = path


@dataclass(frozen=True)
class LutResourceRecord:
    """一次成功验证的资源元数据（进入 ResourceEntry.metadata）。"""

    sha256: str
    size_bytes: int
    format: str                    # cube | 3dl
    lut_size: int                  # 每轴格点数
    title: str
    domain_min: tuple[float, float, float]
    domain_max: tuple[float, float, float]
    has_shaper: bool
    original_filename: str
    source_path: str

    def metadata(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "lut_size": self.lut_size,
            "title": self.title,
            "domain_min": list(self.domain_min),
            "domain_max": list(self.domain_max),
            "has_shaper": self.has_shaper,
        }


@dataclass(frozen=True)
class LutImportResult:
    """单个导入路径的结果（fail-visible：失败/取消/重复都有明确状态）。"""

    path: str
    status: str                    # imported | duplicate | failed | cancelled
    entry: ResourceEntry | None = None
    error: LutResourceError | None = None


def _cancel_requested(cancel_check: Callable[[], bool] | None) -> bool:
    return bool(cancel_check is not None and cancel_check())


def validate_lut_file(path: str, *,
                      cancel_check: Callable[[], bool] | None = None) -> LutResourceRecord:
    """完整验证一个 LUT 文件（解析级检查，非头部探测）。

    Raises:
        LutResourceError: UNSUPPORTED_FORMAT / READ_FAILED / CANCELLED /
            TOO_LARGE / PARSE_FAILED / NON_FINITE / INVALID_DOMAIN。
    """
    if _cancel_requested(cancel_check):
        raise LutResourceError(LutResourceErrorCode.CANCELLED, "导入已取消", path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise LutResourceError(
            LutResourceErrorCode.UNSUPPORTED_FORMAT,
            f"不支持的 LUT 扩展名: {ext or '(无扩展名)'}", path)
    try:
        size_bytes = os.path.getsize(path)
    except OSError as exc:
        raise LutResourceError(LutResourceErrorCode.READ_FAILED, str(exc), path) from exc
    if size_bytes > MAX_LUT_FILE_BYTES:
        raise LutResourceError(
            LutResourceErrorCode.TOO_LARGE,
            f"文件 {size_bytes} 字节超过上限 {MAX_LUT_FILE_BYTES}", path)
    try:
        lut = load_lut(path)
    except FileNotFoundError as exc:
        raise LutResourceError(LutResourceErrorCode.READ_FAILED, str(exc), path) from exc
    except OSError as exc:
        raise LutResourceError(LutResourceErrorCode.READ_FAILED, str(exc), path) from exc
    except (ValueError, IndexError, MemoryError) as exc:
        raise LutResourceError(LutResourceErrorCode.PARSE_FAILED, str(exc), path) from exc
    if _cancel_requested(cancel_check):
        raise LutResourceError(LutResourceErrorCode.CANCELLED, "导入已取消", path)
    if lut.size > MAX_LUT_GRID_SIZE:
        raise LutResourceError(
            LutResourceErrorCode.TOO_LARGE,
            f"网格 {lut.size}³ 超过上限 {MAX_LUT_GRID_SIZE}³", path)
    import numpy as np
    finite = bool(np.isfinite(lut.table).all())
    if not finite or (lut.shaper_1d is not None
                      and not bool(np.isfinite(lut.shaper_1d).all())):
        raise LutResourceError(
            LutResourceErrorCode.NON_FINITE, "表内含 NaN/Inf 数据", path)
    dmin, dmax = lut.domain_min, lut.domain_max
    if any(dmax[i] <= dmin[i] for i in range(3)):
        raise LutResourceError(
            LutResourceErrorCode.INVALID_DOMAIN,
            f"DOMAIN 区间非法: min={dmin} max={dmax}", path)
    return LutResourceRecord(
        sha256=sha256_file(path),
        size_bytes=size_bytes,
        format=lut.format,
        lut_size=lut.size,
        title=lut.title or os.path.splitext(os.path.basename(path))[0],
        domain_min=tuple(float(v) for v in dmin),
        domain_max=tuple(float(v) for v in dmax),
        has_shaper=lut.shaper_1d is not None,
        original_filename=os.path.basename(path),
        source_path=os.path.abspath(path),
    )


def lut_resource_id(sha256_hex: str) -> str:
    """资源 ID 只依赖内容（128 bit 前缀足够防碰撞），与机器路径无关。"""
    return f"lut_{sha256_hex[:32]}"


class LutResourceSession:
    """进程级 LUT 资源会话：导入暂存 + 工程索引合并 + 保存载荷生成。

    线程模型：RLock 保护；导入可由后台线程执行（验证/哈希/拷贝均无 Qt）。
    """

    def __init__(self, staging_root: str | None = None) -> None:
        self._lock = threading.RLock()
        self._staging_root = staging_root or tempfile.mkdtemp(
            prefix="topos-lut-import-")
        # resource_id → 条目（工程加载条目 + 会话导入条目）
        self._entries: dict[str, ResourceEntry] = {}
        # archive_path → 本机暂存源（仅会话导入的新条目需要写包时流式拷入）
        self._staged_sources: dict[str, str] = {}
        self._diagnostics: list[str] = []

    # ── 工程生命周期 ───────────────────────────────────────────────
    def bind_project(self, resolver: Any) -> None:
        """工程加载：合并包内资源索引（全部 kind，防保存时丢条目）。

        无资源索引的旧包/空工程 → 清空会话条目（防跨工程串注册）。
        """
        with self._lock:
            self._clear_staging()
            self._entries.clear()
            index = getattr(resolver, "index", None) if resolver is not None else None
            if index is not None:
                for entry in getattr(index, "entries", {}).values():
                    if isinstance(entry, ResourceEntry):
                        self._entries[entry.resource_id] = entry

    def reset(self) -> None:
        """工程关闭/换会话：清条目并清理暂存目录。"""
        with self._lock:
            self._entries.clear()
            self._clear_staging()

    def _clear_staging(self) -> None:
        self._staged_sources.clear()
        shutil.rmtree(self._staging_root, ignore_errors=True)
        try:
            os.makedirs(self._staging_root, exist_ok=True)
        except OSError:
            pass
        self._diagnostics.clear()

    # ── 导入 ───────────────────────────────────────────────────────
    def import_files(
        self, paths: Iterable[str], *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[LutImportResult]:
        """批量导入：验证 → 去重 → 原子暂存拷贝 → 注册条目。

        单个路径失败/取消不影响其他路径（逐项返回结果）；任何失败都不
        留半条目（暂存拷贝成功才注册）。
        """
        results: list[LutImportResult] = []
        for raw in paths:
            path = str(raw)
            if _cancel_requested(cancel_check):
                results.append(LutImportResult(
                    path, "cancelled",
                    error=LutResourceError(LutResourceErrorCode.CANCELLED,
                                           "导入已取消", path)))
                continue
            try:
                record = validate_lut_file(path, cancel_check=cancel_check)
            except LutResourceError as exc:
                results.append(LutImportResult(path, "failed", error=exc))
                self._diagnostics.append(f"导入失败 {path}: {exc}")
                continue
            with self._lock:
                existing = self._by_sha(record.sha256)
                if existing is not None:
                    results.append(LutImportResult(
                        path, "duplicate", entry=existing))
                    continue
                try:
                    staged = self._stage_copy(record)
                except OSError as exc:
                    error = LutResourceError(LutResourceErrorCode.WRITE_FAILED,
                                             str(exc), path)
                    results.append(LutImportResult(path, "failed", error=error))
                    self._diagnostics.append(f"暂存失败 {path}: {exc}")
                    continue
                entry = ResourceEntry(
                    resource_id=lut_resource_id(record.sha256),
                    kind="lut",
                    archive_path=staged_archive_path(record),
                    size_bytes=record.size_bytes,
                    sha256=record.sha256,
                    original_filename=record.original_filename,
                    mime=f"application/x-lut-{record.format}",
                    roles=("lut",),
                    metadata=record.metadata(),
                )
                self._entries[entry.resource_id] = entry
                self._staged_sources[entry.archive_path] = staged
            results.append(LutImportResult(path, "imported", entry=entry))
        return results

    def _by_sha(self, sha256_hex: str) -> ResourceEntry | None:
        for entry in self._entries.values():
            if entry.sha256 == sha256_hex:
                return entry
        return None

    def _stage_copy(self, record: LutResourceRecord) -> str:
        """原子拷入暂存目录（temp + os.replace；失败无残留半文件）。"""
        os.makedirs(self._staging_root, exist_ok=True)
        destination = os.path.join(
            self._staging_root, f"{record.sha256}{os.path.splitext(record.source_path)[1].lower()}")
        fd, temp = tempfile.mkstemp(dir=self._staging_root, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as target, open(record.source_path, "rb") as source:
                shutil.copyfileobj(source, target)
            os.replace(temp, destination)
        except OSError:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        return destination

    # ── 目录/持久化投影 ────────────────────────────────────────────
    def entries(self) -> list[ResourceEntry]:
        with self._lock:
            return [self._entries[rid] for rid in sorted(self._entries)]

    def lut_entries(self) -> list[ResourceEntry]:
        return [e for e in self.entries() if e.kind == "lut"]

    def catalog(self) -> list[dict[str, Any]]:
        """LUT 浏览器条目投影（id = topos-res:// URI，机器无关）。"""
        projections: list[dict[str, Any]] = []
        for entry in self.lut_entries():
            meta = entry.metadata or {}
            projections.append({
                "id": entry.uri,
                "kind": "lut",
                "name": meta.get("title") or os.path.splitext(
                    entry.original_filename or entry.resource_id)[0],
                "category": "LUT",
                "description": (
                    f"{meta.get('format', 'lut')} · {meta.get('lut_size', '?')}³"
                    + (" · 1D shaper" if meta.get("has_shaper") else "")),
                "available": True,
                "resource_uri": entry.uri,
                "sha256": entry.sha256,
                "original_filename": entry.original_filename,
            })
        return projections

    def save_payload(self) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        """保存载荷：(resources_data, resource_files)。

        - 会话为空 → (None, None)：保存方保持"无资源协议"语义，包内已有
          索引/文件由 payload Phase B 原样保留。
        - 有条目 → 合并索引（工程加载条目 + 会话导入）；仅新导入条目需要
          resource_files（工程加载条目的文件已在包 payload 缓存里）。
        """
        with self._lock:
            if not self._entries:
                return None, None
            index = ResourceIndex(entries=dict(self._entries))
            files = dict(self._staged_sources)
            return index.to_dict(), (files or None)

    # ── 迁移与重定位 ───────────────────────────────────────────────
    def migrate_legacy(self, path: str, *,
                       cancel_check: Callable[[], bool] | None = None,
                       ) -> tuple[str | None, str]:
        """旧绝对路径 → 工程资源引用。

        Returns:
            (uri, status)：文件存在且验证通过 → (topos-res://…, "migrated")；
            已注册同内容 → (既有 uri, "duplicate")；缺失/失败 →
            (None, "missing"|"failed")——旧字段由调用方保留，绝不静默移除。
        """
        if not isinstance(path, str) or not path:
            return None, "missing"
        if path.startswith("topos-res://"):
            return path, "already_reference"
        if not os.path.isfile(path):
            return None, "missing"
        result = self.import_files([path], cancel_check=cancel_check)[0]
        if result.status in ("imported", "duplicate") and result.entry is not None:
            return result.entry.uri, ("migrated" if result.status == "imported"
                                      else "duplicate")
        return None, "failed"

    def status_of(self, reference: str) -> str:
        """引用当前状态：registered | missing | unregistered。"""
        if not isinstance(reference, str) or not reference:
            return "missing"
        if reference.startswith("topos-res://"):
            with self._lock:
                body = reference[len("topos-res://"):]
                if body.startswith("sha256-"):
                    return ("registered"
                            if self._by_sha(body[len("sha256-"):]) else "missing")
                return "registered" if body in self._entries else "missing"
        return "registered" if os.path.isfile(reference) else "missing"

    def diagnostics(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._diagnostics)


def staged_archive_path(record: LutResourceRecord) -> str:
    """包内路径：resources/lut/<sha256><ext>（与 make_resource_entry 同构）。"""
    ext = os.path.splitext(record.original_filename or record.source_path)[1].lower()
    return f"resources/lut/{record.sha256}{ext}"


# ── 进程级会话（对齐 set_active_project_resolver 的单例模式） ─────────
_SESSION_LOCK = threading.Lock()
_SESSION: LutResourceSession | None = None


def get_lut_resource_session() -> LutResourceSession:
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = LutResourceSession()
    return _SESSION
