"""
ToposProjectIO - 统一工程包序列化器。

唯一工程读写入口，保存/加载 .topos ZIP 包。
统一使用 .topos ZIP 工程包。

.topos ZIP 包内部结构：
project.topos
├── manifest.json           # 包元数据（格式标识、版本、工程模式）
├── project.json            # 工程核心索引（引用各子目录数据）
├── edit/
│   ├── media_pool.json     # 完整 MediaAsset 数据
│   ├── bins.json           # 完整 ProjectBin 数据
│   ├── timelines.json      # 完整 TimelineSequenceRef 数据
│   └── timeline_sequences.json  # TimelineSequence 数据（P3）
├── color/
│   ├── color_stacks.json   # 完整 ColorStackData 数据
│   └── looks/              # Look 预设存储目录
├── composite/
│   ├── graphs.json         # 完整 CompositeGraphData 数据
│   └── viewer_state.json   # 合成查看器状态
├── deliver/
│   └── presets.json        # 导出预设
├── ui/
│   └── workspace_state.json  # UI 状态
├── assets/
│   ├── baked/              # AI/调色烘焙结果
│   ├── ai_results/         # AI 处理结果
│   ├── thumbnails/         # 缩略图缓存
│   └── proxies/            # 代理文件
└── cache_manifest.json     # 资产缓存清单

设计原则：
- 唯一入口，所有工程读写都通过 ToposProjectIO
- ZIP 包格式，便于跨平台传输
- 支持多种序列化格式：JSON（默认）、MessagePack（可选）
- 路径相对化：素材路径存储为相对路径，加载时转为绝对路径
- 离线素材检测：加载时检查素材是否存在

参考文档：
- docs/library_media_pool_project_architecture_plan_2026-05-19.md
"""

from __future__ import annotations

import json
import os
import zipfile
import tempfile
import shutil
import threading
import time
import uuid
import stat
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - annotation-only imports
    from PyQt6.QtWidgets import QWidget

    from .offline_asset_manager import OfflineAssetReport

from .manifest import ToposManifest
from .topos_project import (
    ToposProject,
    MediaAssetRef,
    is_virtual_asset_path,
)
from .payload_cache import ProjectArchivePayload, ProjectArchiveReadError
from .package_spec import classify_package_versions
from .resource_resolver import (
    RESOURCE_INDEX_ARCHIVE_PATH,
    ProjectResourceResolver,
    ResourceIndex,
    ResourceResolutionError,
    is_package_cache_path as _is_package_cache_path,
    set_active_project_resolver,
)

logger = logging.getLogger(__name__)

try:  # POSIX advisory file locks
    import fcntl
except ImportError:  # pragma: no cover - win32
    fcntl = None  # type: ignore[assignment]

try:  # Windows advisory file locks
    import msvcrt
except ImportError:  # pragma: no cover - posix
    msvcrt = None  # type: ignore[assignment]


# ================================================================
# 解压上限（AUD-37104 zip-bomb 防护）
# ================================================================

#: 单个 .topos 工程包允许的最大成员数。合法工程包（含全部模块 JSON 与
#: 资产文件）远低于该量级；超过即视为恶意或损坏包，拒绝解压。
MAX_ARCHIVE_ENTRIES: int = 50_000

#: 解压允许写入的最大总字节数（未压缩）。上限取得宽松（4K EXR 烘焙资产
#: 可达数 GB），但足以拦截 PB 级 zip bomb 耗尽磁盘。
MAX_ARCHIVE_TOTAL_BYTES: int = 32 * 1024 * 1024 * 1024

#: 解压流式拷贝的单块大小。
_EXTRACT_CHUNK_BYTES: int = 1024 * 1024


def _lock_advisory(fd: int) -> None:
    """在打开的文件描述符上加排他咨询锁（跨进程互斥）。"""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    elif msvcrt is not None:  # pragma: no cover - windows
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)


def _unlock_advisory(fd: int) -> None:
    """释放 _lock_advisory 加上的咨询锁。"""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - windows
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class _ArchiveLockToken:
    """一次成功加锁的凭据；``release()`` 归还全部持有的资源。"""

    def __init__(
        self,
        owner: "_ArchiveLock",
        path: str,
        *,
        file_descriptor: int,
        holds_file_lock: bool,
    ) -> None:
        self._owner = owner
        self._path = path
        self._file_descriptor = file_descriptor
        self._holds_file_lock = holds_file_lock
        self._released = False

    def release(self) -> None:
        """释放锁；重复调用无副作用。"""
        if self._released:
            return
        self._released = True
        self._owner._release(self._path, self._file_descriptor, self._holds_file_lock)


class _ArchiveLock:
    """按 .topos 归档路径串行化 load()/save() 的咨询锁（AUD-37102）。

    三层结构：

    1. 每路径 ``threading.Lock`` —— 本进程内线程互斥（save 的
       读-改-写与 load 的缓存目录替换不再交错）。
    2. 归档旁的 ``.<name>.lock`` 咨询文件锁（flock/LockFile）——
       跨进程互斥（主程序与助手进程同时保存同一工程）。
    3. 同线程重入登记 —— save 回调间接触发同路径 load 时不会自死锁。

    注意：锁文件会常驻工程目录（体积极小，删除反而引入竞态）。
    """

    def __init__(self) -> None:
        self._registry_mutex = threading.Lock()
        self._path_locks: dict[str, threading.Lock] = {}
        self._depths: dict[tuple[int, str], int] = {}

    def _path_lock(self, path: str) -> threading.Lock:
        with self._registry_mutex:
            lock = self._path_locks.get(path)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[path] = lock
            return lock

    def acquire(self, filepath: str) -> _ArchiveLockToken:
        """阻塞获取 *filepath* 的排他锁，返回凭据。"""
        path = os.path.abspath(filepath)
        key = (threading.get_ident(), path)

        with self._registry_mutex:
            depth = self._depths.get(key, 0)
            if depth > 0:
                # 同线程重入：外层凭据仍持有 OS 锁，只加深计数。
                self._depths[key] = depth + 1
                return _ArchiveLockToken(
                    self, path, file_descriptor=-1, holds_file_lock=False
                )

        path_lock = self._path_lock(path)
        path_lock.acquire()

        fd = -1
        holds_file_lock = False
        lock_path = os.path.join(
            os.path.dirname(path), f".{os.path.basename(path)}.lock"
        )
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            _lock_advisory(fd)
            holds_file_lock = True
        except OSError as exc:
            # 拿不到跨进程锁（如网络盘不支持 flock）：降级为仅进程内
            # 互斥，不阻塞正常保存。
            logger.warning("Archive file lock unavailable for %s: %s", path, exc)
            if fd >= 0:
                os.close(fd)
                fd = -1

        with self._registry_mutex:
            self._depths[key] = 1
        return _ArchiveLockToken(
            self, path, file_descriptor=fd, holds_file_lock=holds_file_lock
        )

    def _release(self, path: str, fd: int, holds_file_lock: bool) -> None:
        key = (threading.get_ident(), path)
        with self._registry_mutex:
            depth = self._depths.get(key, 0)
            if depth > 1:
                # 仍有外层持有。
                self._depths[key] = depth - 1
                return
            self._depths.pop(key, None)

        if fd >= 0:
            try:
                if holds_file_lock:
                    _unlock_advisory(fd)
            except OSError as exc:
                logger.warning("Archive file unlock failed for %s: %s", path, exc)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._path_lock(path).release()


#: 进程级单例：所有 ToposProjectIO.save/load 共享。
_ARCHIVE_LOCK = _ArchiveLock()


def _current_product_name() -> str:
    """Return the current product name used for manifest metadata."""
    try:
        from src.products import get_current_product

        return get_current_product().name
    except Exception:
        return "topos-studio"


def _present_modules_from_payload(payload: ProjectArchivePayload) -> tuple[str, ...]:
    """Infer present feature modules from archive payload paths."""
    present: set[str] = set()
    for path in payload.module_payloads:
        if path.startswith("color/"):
            present.add("color")
        elif path.startswith("edit/"):
            present.add("edit")
        elif path.startswith("composite/"):
            present.add("composite")
        elif path.startswith("deliver/"):
            present.add("deliver")
        elif path.startswith("teaching_animation/"):
            present.add("teaching_animation")
        elif path.startswith("ai_workflow/"):
            present.add("ai_workflow")
        elif path.startswith("ui/"):
            present.add("ui")
        elif path.startswith("resources/"):
            present.add("resources")
    return tuple(sorted(present))


def _is_teaching_animation_enabled() -> bool:
    """Resolve the optional flag without importing the optional feature."""
    from src.shared.feature_flags import load_feature_flags

    return load_feature_flags().is_enabled("teaching_animation")


# ================================================================
# ToposProjectIOConfig - 配置选项
# ================================================================

@dataclass(frozen=True)
class ToposProjectIOConfig:
    """
    ToposProjectIO 配置选项。

    Attributes:
        serialization_format: 序列化格式（json 或 msgpack）
        compress_level: ZIP 压缩级别（0-9，默认 6）
        compress_project_file: 是否对 project.json 使用 gzip 压缩
        relative_paths: 是否将素材路径转为相对路径
        include_cache_assets: 是否包含缓存资产（baked, thumbnails, proxies）
        max_cache_size_mb: 最大缓存资产大小（MB）
    """
    serialization_format: str = "json"  # json, msgpack
    compress_level: int = 6  # ZIP 压缩级别（0-9）
    compress_project_file: bool = False  # 是否对 project.json 使用 gzip
    relative_paths: bool = True  # 是否将素材路径转为相对路径
    include_cache_assets: bool = True  # 是否包含缓存资产
    max_cache_size_mb: int = 500  # 最大缓存资产大小


# ================================================================
# CacheManifest - 缓存资产清单
# ================================================================

@dataclass(frozen=True)
class CacheManifest:
    """
    缓存资产清单。

    记录 assets/ 目录下的所有缓存文件及其元数据。

    Attributes:
        baked_files: 烘焙文件清单
        ai_result_files: AI 处理结果文件清单
        thumbnail_files: 缩略图文件清单
        proxy_files: 代理文件清单
        total_size_bytes: 总大小（字节）
        created_at: 创建时间戳
    """
    baked_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    ai_result_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    thumbnail_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    proxy_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_size_bytes: int = 0
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化到字典"""
        return {
            "baked_files": self.baked_files,
            "ai_result_files": self.ai_result_files,
            "thumbnail_files": self.thumbnail_files,
            "proxy_files": self.proxy_files,
            "total_size_bytes": self.total_size_bytes,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> CacheManifest:
        """从字典反序列化"""
        return CacheManifest(
            baked_files=data.get("baked_files", {}),
            ai_result_files=data.get("ai_result_files", {}),
            thumbnail_files=data.get("thumbnail_files", {}),
            proxy_files=data.get("proxy_files", {}),
            total_size_bytes=data.get("total_size_bytes", 0),
            created_at=data.get("created_at", 0.0),
        )


# ================================================================
# ToposProjectIO - 统一工程读写入口
# ================================================================

class ToposProjectIO:
    """
    统一工程读写入口。

    保存/加载 .topos ZIP 包，唯一入口。

    主要方法：
    - save(): 保存工程到 .topos 文件
    - load(): 从 .topos 文件加载工程
    - is_topos_file(): 检查是否是 .topos 文件
    - check_offline_assets(): 检查离线素材
    - relink_assets(): 重链接素材
    """

    TOPOS_EXTENSION = ".topos"

    # 子目录结构
    SUB_DIRS = [
        "edit",
        "color",
        "composite",
        "deliver",
        "ui",
        "assets/baked",
        "assets/ai_results",
        "assets/thumbnails",
        "assets/proxies",
        "teaching_animation",
        "ai_workflow",
    ]

    # 子目录 JSON 文件名
    SUB_JSON_FILES = {
        "edit/media_pool.json": "assets",
        "edit/bins.json": "bins",
        "edit/timelines.json": "timelines",
        "edit/smart_bins.json": "smart_bins",
        "edit/timeline_sequences.json": "timeline_sequences",
        "color/color_stacks.json": "color_stacks",
        "color/looks.json": "looks",
        "composite/graphs.json": "graphs",
        "composite/viewer_state.json": "viewer_state",
        "deliver/presets.json": "deliver_presets",
        "ui/workspace_state.json": "workspace_state",
        "teaching_animation/scenes.json": "teaching_animation",
        "teaching_animation/render_cache_manifest.json": "teaching_animation_render_cache_manifest",
        "ai_workflow/workflows.json": "ai_workflows",
    }

    @staticmethod
    def get_topos_path(project_dir: str, project_name: str) -> str:
        """
        生成 .topos 文件路径。

        Args:
            project_dir: 工程目录
            project_name: 工程名称

        Returns:
            完整的 .topos 文件路径
        """
        safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in project_name)
        return os.path.join(project_dir, f"{safe_name}{ToposProjectIO.TOPOS_EXTENSION}")

    @staticmethod
    def is_topos_file(filepath: str) -> bool:
        """检查文件是否是 .topos 工程包或其恢复副本。

        自动保存和备份仍然是完整的 Topos ZIP 包，只是在 canonical
        ``.topos`` 后追加了 ``.autosave`` / ``.bak``。加载入口需要接受
        这些恢复路径，否则合法的自动保存文件会被误判为旧 JSON 格式。
        """
        normalized = filepath.lower()
        return normalized.endswith((
            ToposProjectIO.TOPOS_EXTENSION,
            f"{ToposProjectIO.TOPOS_EXTENSION}.autosave",
            f"{ToposProjectIO.TOPOS_EXTENSION}.bak",
        ))

    @staticmethod
    def validate_archive_limits(zf: zipfile.ZipFile) -> int:
        """校验归档的成员数与声明的未压缩总大小（AUD-37104）。

        Args:
            zf: 打开于读模式的 ZipFile

        Returns:
            声明的未压缩总字节数

        Raises:
            ValueError: 成员数或声明的总大小超过上限。声明值来自 ZIP
                中央目录，恶意头可以虚报小值，因此实际写入字节数仍在
                :meth:`_extract_archive_to_cache` 流式拷贝时二次强制。
        """
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ValueError(
                f"Project archive declares {len(infos)} entries "
                f"(limit {MAX_ARCHIVE_ENTRIES}); refusing extraction"
            )
        declared_total = sum(info.file_size for info in infos)
        if declared_total > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError(
                f"Project archive declares {declared_total} uncompressed bytes "
                f"(limit {MAX_ARCHIVE_TOTAL_BYTES}); refusing extraction"
            )
        return declared_total

    @staticmethod
    def _extract_archive_to_cache(zf: zipfile.ZipFile, destination: str) -> None:
        """Safely materialize an archive into an otherwise empty cache dir.

        AUD-37104: entry count and total uncompressed size are capped —
        declared values up front, actual written bytes during the streaming
        copy — so a malicious or corrupt archive cannot exhaust disk space.
        """
        ToposProjectIO.validate_archive_limits(zf)
        root = os.path.abspath(destination)
        written_total = 0
        for info in zf.infolist():
            # Project packages do not need symlinks. Reject them because they
            # may resolve outside the cache after extraction.
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"Project archive contains a symlink: {info.filename}")

            target = os.path.abspath(os.path.join(root, info.filename))
            if os.path.commonpath((root, target)) != root:
                raise ValueError(f"Project archive path escapes cache: {info.filename}")

            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue

            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info, "r") as source, open(target, "wb") as output:
                while True:
                    chunk = source.read(_EXTRACT_CHUNK_BYTES)
                    if not chunk:
                        break
                    written_total += len(chunk)
                    if written_total > MAX_ARCHIVE_TOTAL_BYTES:
                        raise ValueError(
                            "Project archive exceeds the extraction size limit "
                            f"of {MAX_ARCHIVE_TOTAL_BYTES} bytes (zip bomb?)"
                        )
                    output.write(chunk)

    @staticmethod
    def save(
        project: ToposProject,
        filepath: str,
        config: ToposProjectIOConfig = ToposProjectIOConfig(),
        full_assets_data: dict[str, Any] | None = None,
        full_bins_data: dict[str, Any] | None = None,
        full_timelines_data: dict[str, Any] | None = None,
        full_color_stacks_data: dict[str, Any] | None = None,
        full_compositions_data: dict[str, Any] | None = None,
        comp_clips_data: dict[str, Any] | None = None,
        smart_bins_data: dict[str, Any] | None = None,
        timeline_sequences_data: dict[str, Any] | None = None,
        deliver_presets_data: dict[str, Any] | None = None,
        ui_state_data: dict[str, Any] | None = None,
        cache_assets_dir: str | None = None,
        ai_session_data: dict[str, Any] | None = None,
        ai_plans_data: dict[str, Any] | None = None,
        ai_jobs_data: dict[str, Any] | None = None,
        ai_analysis_data: dict[str, Any] | None = None,
        ai_runtime_data: dict[str, Any] | None = None,
        teaching_animation_data: Any | None = None,
        ai_trace_data: dict[str, Any] | None = None,
        ai_plugins_data: dict[str, Any] | None = None,
        ai_workflows_data: dict[str, Any] | None = None,
        resources_data: dict[str, Any] | None = None,
        resource_files: dict[str, str] | None = None,
        backing_store: Any | None = None,
        should_cancel: Any | None = None,
    ) -> bool:
        """
        保存工程到 .topos ZIP 包。

        Key behaviour: unknown module data from other products is preserved.
        If the file already exists, ALL existing entries are read into a
        ProjectArchivePayload first.  Then the current product's modules
        overwrite their respective payloads, and everything else is written
        back unchanged.

        Args:
            project: ToposProject 实例
            filepath: .topos 文件路径
            config: 配置选项
            full_assets_data: 完整素材数据（用于 edit/media_pool.json）
            full_bins_data: 完整素材箱数据（用于 edit/bins.json）
            full_timelines_data: 完整时间线引用数据（用于 edit/timelines.json）
            full_color_stacks_data: 完整调色栈数据（用于 color/color_stacks.json）
            full_compositions_data: 完整合成图数据（用于 composite/graphs.json）
            comp_clips_data: 媒体池 Comp Clip 元数据（用于 edit/comp_clips.json；
                传入空 dict 表示清除旧数据，None 表示保留已有内容）
            timeline_sequences_data: 时间线序列数据（用于 edit/timeline_sequences.json）
            deliver_presets_data: 导出预设数据（用于 deliver/presets.json）
            ui_state_data: UI 状态数据（用于 ui/workspace_state.json）
            cache_assets_dir: 缓存资产源目录（用于复制 baked/thumbnails/proxies）
            ai_session_data: AI 会话目录数据（用于 ai/session_catalog.json；
                向后兼容也写 ai/session.json v1）
            ai_plans_data: AI plan 仓库数据（用于 ai/plans.json）
            ai_jobs_data: AI JobRegistry 快照（用于 ai/jobs.json；活动 job
                在加载时默认安全终止，不恢复孤儿 worker）
            ai_analysis_data: AI 素材证据缓存（用于 ai/analysis.json；素材指纹
                变化后由 AnalysisService 失效，不恢复为可用证据）
            ai_runtime_data: AI HarnessRuntime 快照（用于 ai/runtime.json；活动
                run 在恢复时标记为中断，不恢复孤儿 worker）
            ai_trace_data: AI 运行轨迹（用于 ai/trace.json；只保存脱敏、有界事件）
            ai_plugins_data: AI 插件事实与 checkpoint journal（用于 ai/plugins.json；
                不恢复 active 状态或 approval）
            teaching_animation_data: 可选 Teaching Animation 场景 payload。
                未安装模块时不传入该参数，已有 payload 和缓存仍会被保留。
            ai_workflows_data: 可选 AI Workflow 存储 payload（用于
                ai_workflow/workflows.json；用户编辑的节点图）。None 时保留
                已有内容不覆盖。
            resources_data: DESK-01B 资源索引（resources/resources.json）。
                None = 本产品不含资源协议：已有索引原样保留。
            resource_files: DESK-01B {archive_path: 本机源文件}，写包时
                流式拷入（ZIP_STORED，不驻内存）。
            backing_store: DESK-03A 来源包载荷快照（ArchiveBackingStore）。
                目标 ≠ 来源（另存/复制/自动保存）时作为保留基底并入，
                目标已有条目优先；来源文件丢失时保存受控失败。
            should_cancel: DESK-03D 取消检查回调（返回 True 即中止）。
                在阶段边界检查（锁后/payload 读后/模块写备后/晋升前）；
                取消 → 受控返回 False，目标文件与临时文件均不残留。
                写入流中途的取消表现为写失败（同一清理路径，不留半包）。

        Returns:
            True if successful, False otherwise
        """
        # AUD-37102: serialize the read-modify-write cycle (Phase B payload
        # read ... final atomic replace) per archive, in-process and
        # cross-process, so concurrent saves cannot drop each other's
        # module updates.
        archive_token = _ARCHIVE_LOCK.acquire(filepath)
        try:
            # DESK-03D：阶段边界取消检查（详见 should_cancel 契约）
            if should_cancel is not None and should_cancel():
                logger.info("Save cancelled before payload read: %s", filepath)
                return False
            # 获取工程目录（用于计算相对路径）
            project_dir = os.path.dirname(os.path.abspath(filepath))

            # ---- Phase B: load existing payload to preserve unknown modules ----
            payload = ProjectArchivePayload.load_from_file(filepath)
            if should_cancel is not None and should_cancel():
                logger.info("Save cancelled after payload read: %s", filepath)
                return False

            # DESK-03A/B：另存/复制/自动保存（目标 ≠ 来源）时，来源 backing
            # 作为保留基底并入——目标已有条目优先，backing 只补缺。原地
            # 保存（同路径）不参与（目标本身即完整最新状态）。
            if backing_store is not None and not backing_store.is_same_target(
                    filepath):
                from .archive_backing import merge_backing_payload
                merge_backing_payload(payload, backing_store.payload)

            # 1. Overwrite manifest with fresh data
            manifest = ToposManifest.create(
                project_id=project.project_id,
                project_mode=project.project_mode,
                features=project.features,
                primary_asset_id=project.primary_asset_id,
                active_timeline_id=project.active_timeline_id,
            )
            # AUD-37101: merge direction matters. Starting from the existing
            # manifest and overlaying the fresh one keeps unknown/future keys,
            # but the overlay would also let create() reset creation and
            # provenance fields (create() stamps created_at=now,
            # created_by=default product, required/optional_features=empty).
            # Restore those from the existing archive so every save stops
            # rewriting the project's origin.
            existing_manifest = payload.manifest
            if existing_manifest:
                manifest_data = dict(existing_manifest)
                manifest_data.update(manifest.to_dict())
                for preserved_key in (
                    "created_at",
                    "created_by",
                    "required_features",
                    "optional_features",
                ):
                    if preserved_key in existing_manifest:
                        manifest_data[preserved_key] = existing_manifest[preserved_key]
                # Never downgrade version stamps written by a newer writer.
                existing_schema_version = existing_manifest.get("schema_version")
                if (
                    isinstance(existing_schema_version, int)
                    and existing_schema_version > manifest_data["schema_version"]
                ):
                    manifest_data["schema_version"] = existing_schema_version
                existing_edit_schema = existing_manifest.get(
                    "edit_content_schema_version"
                )
                if (
                    isinstance(existing_edit_schema, int)
                    and existing_edit_schema
                    > manifest_data["edit_content_schema_version"]
                ):
                    manifest_data["edit_content_schema_version"] = existing_edit_schema
            else:
                manifest_data = manifest.to_dict()

            manifest_data["modified_at"] = time.time()
            manifest_data["last_saved_by"] = _current_product_name()

            # 2. Overwrite project.json
            project_data = project.to_dict()
            if config.relative_paths:
                project_data = ToposProjectIO._relativize_project_paths(project_data, project_dir)
            # DESK-04A：project.json 的未知键随 Phase B 基底合并——
            # 目标/来源包里未来写入者的顶层键与素材 ref 级键穿过本次
            # 保存。已知键与 ref 集合由新数据作主：**已删除的素材不会
            # 被旧包复活**（只在两侧都在场的 ref 内合并未知键）。
            existing_pj = payload.project_json
            if isinstance(existing_pj, dict) and existing_pj:
                for key, value in existing_pj.items():
                    if key == "assets" and isinstance(value, dict) \
                            and isinstance(project_data.get("assets"), dict):
                        for asset_id, old_ref in value.items():
                            fresh_ref = project_data["assets"].get(asset_id)
                            if isinstance(old_ref, dict) and isinstance(fresh_ref, dict):
                                merged = dict(old_ref)
                                merged.update(fresh_ref)
                                project_data["assets"][asset_id] = merged
                    elif key not in project_data:
                        project_data[key] = value
            payload.set_project_json(project_data)

            # 3. Overwrite only the modules that the current product provides
            if full_assets_data is not None:
                assets_data = full_assets_data
                if config.relative_paths:
                    assets_data = ToposProjectIO._relativize_assets_paths(assets_data, project_dir)
                payload.set_module("edit/media_pool.json", assets_data)

            if full_bins_data is not None:
                payload.set_module("edit/bins.json", full_bins_data)

            if full_timelines_data is not None:
                payload.set_module("edit/timelines.json", full_timelines_data)

            if comp_clips_data is not None:
                payload.set_module("edit/comp_clips.json", comp_clips_data)
            if smart_bins_data is not None:
                payload.set_module("edit/smart_bins.json", smart_bins_data)

            if full_color_stacks_data is not None:
                payload.set_module("color/color_stacks.json", full_color_stacks_data)

            if full_compositions_data is not None:
                payload.set_module("composite/graphs.json", full_compositions_data)
                if ui_state_data and "composite_viewer_state" in ui_state_data:
                    payload.set_module(
                        "composite/viewer_state.json",
                        ui_state_data.get("composite_viewer_state", {}),
                    )

            if timeline_sequences_data is not None:
                payload.set_module("edit/timeline_sequences.json", timeline_sequences_data)

            if deliver_presets_data is not None:
                payload.set_module("deliver/presets.json", deliver_presets_data)

            if ui_state_data is not None:
                payload.set_module("ui/workspace_state.json", ui_state_data)

            # AI 编辑子系统（TAEP）payload：会话目录 + plan 仓库。
            # ai_session_data 同时写 v1 session.json（旧版本向后兼容）+ v2 catalog。
            if ai_session_data is not None:
                payload.set_module("ai/session_catalog.json", ai_session_data)
                # 若调用方提供 v1 快照字段则一并保留；否则只写 catalog。
                v1_snapshot = ai_session_data.get("legacy_snapshot") or ai_session_data
                payload.set_module("ai/session.json", v1_snapshot)
            if ai_plans_data is not None:
                payload.set_module("ai/plans.json", ai_plans_data)
            if ai_jobs_data is not None:
                payload.set_module("ai/jobs.json", ai_jobs_data)
            if ai_analysis_data is not None:
                payload.set_module("ai/analysis.json", ai_analysis_data)
            if ai_runtime_data is not None:
                payload.set_module("ai/runtime.json", ai_runtime_data)
            if ai_trace_data is not None:
                payload.set_module("ai/trace.json", ai_trace_data)
            if ai_plugins_data is not None:
                payload.set_module("ai/plugins.json", ai_plugins_data)

            # Optional module payload.  Deliberately do not remove this path
            # when the module is disabled or physically absent: the payload
            # cache writes unknown/future module data back unchanged.
            if teaching_animation_data is not None:
                teaching_payload = teaching_animation_data
                # Keep shared project IO independent from the optional
                # feature package while accepting its typed payload through a
                # narrow duck-typed serialization boundary.
                if hasattr(teaching_payload, "to_dict"):
                    teaching_payload = teaching_payload.to_dict()
                if not isinstance(teaching_payload, dict):
                    raise TypeError("teaching_animation_data must serialize to a dict")
                payload.set_module(
                    "teaching_animation/scenes.json",
                    teaching_payload,
                )

            # AI Workflow 存储（plan §13：工作流存进项目文件）。
            # None = 模块未启用/无图：保留已有条目（payload cache 写回未知模块）。
            if ai_workflows_data is not None:
                payload.set_module("ai_workflow/workflows.json", ai_workflows_data)

            # DESK-01B：内嵌资源索引 + 资源文件。索引是 JSON 模块；
            # 二进制走 file_members 流式通道（不整读进内存）。None =
            # 不含资源协议的保存方：已有索引/资源原样保留（Phase B）。
            if resources_data is not None:
                payload.set_module(RESOURCE_INDEX_ARCHIVE_PATH, resources_data)
            if resource_files:
                for archive_path, source_path in resource_files.items():
                    payload.add_file_member(archive_path, source_path)

            # 4. Cache manifest (asset cache tracking)
            cache_manifest_data: dict[str, Any] = {}
            existing_cm = payload.get_module("cache_manifest.json")
            if existing_cm:
                cache_manifest_data = existing_cm
            cache_manifest_data["created_at"] = time.time()

            # Copy cache assets if provided
            if cache_assets_dir and config.include_cache_assets:
                with tempfile.TemporaryDirectory() as temp_dir:
                    for sub_dir in ToposProjectIO.SUB_DIRS:
                        os.makedirs(os.path.join(temp_dir, sub_dir), exist_ok=True)
                    cm = ToposProjectIO._copy_cache_assets(
                        cache_assets_dir,
                        temp_dir,
                        config.max_cache_size_mb,
                    )
                    cache_manifest_data.update(cm.to_dict())
                    # Add cache asset files to unknown_files
                    for root, _dirs, files in os.walk(temp_dir):
                        for fname in files:
                            full_path = os.path.join(root, fname)
                            arc_name = os.path.relpath(full_path, temp_dir)
                            with open(full_path, "rb") as fh:
                                payload.unknown_files[arc_name] = fh.read()

            payload.set_module("cache_manifest.json", cache_manifest_data)
            manifest_data["present_modules"] = list(_present_modules_from_payload(payload))
            payload.set_manifest(manifest_data)

            # 5. Write the ZIP
            if should_cancel is not None and should_cancel():
                logger.info("Save cancelled before zip write: %s", filepath)
                return False
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            # Write beside the destination and replace atomically. This keeps
            # readers/backups from observing a half-written ZIP while a save
            # is in progress, and also makes background autosave crash-safe.
            temp_fd, temporary_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(filepath)}.",
                suffix=".tmp",
                dir=os.path.dirname(os.path.abspath(filepath)) or ".",
            )
            os.close(temp_fd)
            try:
                with zipfile.ZipFile(
                    temporary_path, "w", zipfile.ZIP_DEFLATED,
                    compresslevel=config.compress_level,
                ) as zf:
                    payload.write_to_zip(zf)
                if should_cancel is not None and should_cancel():
                    # 已写完临时包但用户已取消：丢弃临时包，不晋升
                    logger.info(
                        "Save cancelled before promote (temp dropped): %s",
                        filepath)
                    return False
                os.replace(temporary_path, filepath)
            finally:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)

            logger.info(f"Project saved (preserving unknown modules): {filepath}")
            return True

        except ProjectArchiveReadError as e:
            # AUD-37103: the destination exists but cannot be read as a ZIP.
            # Refuse to save — writing a fresh package built from an empty
            # payload would silently destroy every other product's module
            # data in the existing archive.
            logger.error(
                "Existing .topos archive %s is unreadable; refusing to "
                "overwrite it and lose preserved module data: %s",
                filepath,
                e,
            )
            return False
        except Exception as e:
            logger.error(f"Failed to save project: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            archive_token.release()

    @staticmethod
    def load(
        filepath: str,
        config: ToposProjectIOConfig = ToposProjectIOConfig(),
        load_full_data: bool = True,
    ) -> tuple[ToposProject | None, dict[str, Any] | None]:
        """
        从 .topos ZIP 包加载工程。

        Args:
            filepath: .topos 文件路径
            config: 配置选项
            load_full_data: 是否加载完整数据（子目录 JSON）

        Returns:
            (ToposProject 实例, 完整数据字典) 或 (None, None) 如果加载失败
            完整数据字典包含：
            - "assets": 完整素材数据
            - "bins": 完整素材箱数据
            - "timelines": 完整时间线引用数据
            - "timeline_sequences": 时间线序列数据
            - "color_stacks": 完整调色栈数据
            - "compositions": 完整合成图数据
            - "comp_clips": 媒体池 Comp Clip 元数据
            - "deliver_presets": 导出预设数据
            - "ui_state": UI 状态数据
            - "cache_manifest": 缓存清单
            - "assets_dir": 资产目录路径（持久 cache 目录）

        Note:
            解压到持久 cache 目录，不会在函数退出后失效。
            cache 目录位于项目文件同级目录的 .topos_cache/<project_filename>/ 中。
        """
        # AUD-37102: serialize against concurrent save()/load() on the same
        # archive — the save's read-modify-write must not interleave with a
        # cache directory swap here.
        archive_token = _ARCHIVE_LOCK.acquire(filepath)
        try:
            # 获取工程目录（用于计算绝对路径和 cache 目录）
            project_dir = os.path.dirname(os.path.abspath(filepath))

            # 使用持久 cache 目录而非临时目录
            # cache 目录位于项目文件同级的 .topos_cache/<project_filename>/ 中
            project_filename = os.path.splitext(os.path.basename(filepath))[0]
            cache_dir = os.path.join(project_dir, ".topos_cache", project_filename)

            # Always load through a fresh staging directory. Reusing the cache
            # directory leaves files removed from a newer archive in place,
            # which can resurrect old timelines, AI plans, or compositions.
            staging_cache_dir = f"{cache_dir}.loading-{uuid.uuid4().hex}"
            retired_cache_dir = f"{cache_dir}.retired-{uuid.uuid4().hex}"
            try:
                os.makedirs(staging_cache_dir, exist_ok=False)
                with zipfile.ZipFile(filepath, 'r') as zf:
                    ToposProjectIO._extract_archive_to_cache(zf, staging_cache_dir)
                # AUD-37102: swap via rename-aside instead of rmtree+rebuild.
                # Readers of cache_dir never see a half-copied directory, the
                # old content stays recoverable until the new one is in
                # place, and the whole section is serialized against other
                # load()/save() calls by the archive lock above.
                if os.path.exists(cache_dir):
                    os.rename(cache_dir, retired_cache_dir)
                try:
                    os.replace(staging_cache_dir, cache_dir)
                except OSError:
                    # Roll the retired directory back into place.
                    if not os.path.exists(cache_dir) and os.path.exists(retired_cache_dir):
                        os.rename(retired_cache_dir, cache_dir)
                    raise
                shutil.rmtree(retired_cache_dir, ignore_errors=True)
            except (OSError, zipfile.BadZipFile, ValueError, RuntimeError) as e:
                shutil.rmtree(staging_cache_dir, ignore_errors=True)
                shutil.rmtree(retired_cache_dir, ignore_errors=True)
                logger.error(f"Failed to extract project archive {filepath}: {e}")
                return None, None

            # 1. 加载 manifest.json
            manifest_path = os.path.join(cache_dir, "manifest.json")
            if not os.path.exists(manifest_path):
                logger.error(f"manifest.json not found in {filepath}")
                return None, None

            manifest_data = ToposProjectIO._read_json(manifest_path, config)
            manifest = ToposManifest.from_dict(manifest_data)

            # 检查 schema 版本
            from src.shared.project.manifest import TOPOS_SCHEMA_VERSION
            if manifest.schema_version > TOPOS_SCHEMA_VERSION:
                logger.warning(
                    f"Schema version {manifest.schema_version} is newer than "
                    f"supported version {TOPOS_SCHEMA_VERSION}. "
                    f"Some features may not work correctly."
                )

            # DESK-01D：版本分级预检（current/legacy/future/unreadable，
            # 契约见 package_spec）。资源索引维度在读取索引后补判。
            version_status = classify_package_versions(manifest_data)
            for dimension, status in version_status.items():
                if status.status == "unreadable":
                    # 低于最小可读版本：受控拒绝打开，绝不静默误读
                    # （此前 v0 会被 from_dict 静默升到 v3）。
                    logger.error(
                        "Project rejected: %s schema version %d is below the "
                        "minimum readable version %d: %s",
                        dimension, status.file_version, status.min_read,
                        filepath,
                    )
                    return None, None
                if status.status == "future":
                    logger.warning(
                        "Project written by a newer app: %s schema v%d > "
                        "current v%d; opening best-effort, save will not "
                        "downgrade (%s)",
                        dimension, status.file_version,
                        status.current_version, filepath,
                    )

            # 2. 加载 project.json
            project_path = os.path.join(cache_dir, "project.json")
            if not os.path.exists(project_path):
                logger.error(f"project.json not found in {filepath}")
                return None, None

            project_data = ToposProjectIO._read_json(project_path, config)
            # 如果启用相对路径，转换为绝对路径
            if config.relative_paths:
                project_data = ToposProjectIO._absolutize_project_paths(project_data, project_dir)

            # Migrate v1 "single_asset" → "standalone_image" before from_dict
            # (ToposProject.__post_init__ rejects "single_asset" in schema v2)
            if project_data.get("project_mode") == "single_asset":
                logger.info("Migrating project_mode from 'single_asset' to 'standalone_image'")
                project_data["project_mode"] = "standalone_image"

            project = ToposProject.from_dict(project_data)

            # Schema v2→v3: reject invalid project modes
            if project.project_mode == "single_asset":
                primary = project.primary_asset
                if primary and primary.media_type == "video":
                    logger.error(
                        "Schema v3 rejection: project_mode='single_asset' with video "
                        "primary asset is no longer supported. "
                        "Video must go through Edit timeline clips."
                    )
                    return None, None
            if project.project_mode == "standalone_image":
                primary = project.primary_asset
                if primary and primary.media_type != "image":
                    logger.error(
                        f"Schema v3 rejection: project_mode='standalone_image' with "
                        f"non-image primary asset (type={primary.media_type})."
                    )
                    return None, None

            # 3. 加载子目录 JSON 文件（如果需要）
            full_data: dict[str, Any] = {}
            if load_full_data:
                # P2-13: manifest 进 full_data（编辑内容迁移按
                # edit_content_schema_version 走集中迁移链）。
                full_data["manifest"] = manifest.to_dict()
                full_data["version_status"] = {
                    dim: {
                        "status": st.status,
                        "file_version": st.file_version,
                        "current_version": st.current_version,
                    }
                    for dim, st in version_status.items()
                }
                # edit/media_pool.json
                media_pool_path = os.path.join(cache_dir, "edit/media_pool.json")
                if os.path.exists(media_pool_path):
                    assets_data = ToposProjectIO._read_json(media_pool_path, config)
                    if config.relative_paths:
                        assets_data = ToposProjectIO._absolutize_assets_paths(assets_data, project_dir)
                    full_data["assets"] = assets_data

                # DESK-01B/01C：内嵌资源索引 + 解析器。索引按内容身份
                # （SHA-256）登记，resolver 校验后才把解包缓存路径交给
                # 解码器；外部路径在线的素材不经此路径。
                resource_index = ResourceIndex()
                resource_index_path = os.path.join(
                    cache_dir, RESOURCE_INDEX_ARCHIVE_PATH)
                if os.path.exists(resource_index_path):
                    resource_index = ResourceIndex.from_dict(
                        ToposProjectIO._read_json(resource_index_path, config))
                # 换工程/旧包无资源：清掉上一工程的资源上下文，防跨工程
                # 串解析（DESK-02B）。
                set_active_project_resolver(None)
                if len(resource_index) > 0:
                    resolver = ProjectResourceResolver(cache_dir, resource_index)
                    full_data["resource_index"] = resource_index.to_dict()
                    full_data["resource_resolver"] = resolver
                    # DESK-02B：设为当前工程资源上下文（LUT/贴图/生成
                    # 资源加载器经 resolve_project_reference 消费）。
                    set_active_project_resolver(resolver)
                    # DESK-01D：资源索引维度分级补判（无索引=legacy-0）
                    version_status = classify_package_versions(
                        manifest_data,
                        resource_index_version=resource_index.schema_version,
                    )
                    full_data["version_status"] = {
                        dim: {
                            "status": st.status,
                            "file_version": st.file_version,
                            "current_version": st.current_version,
                        }
                        for dim, st in version_status.items()
                    }
                    if "assets" in full_data:
                        embedded = ToposProjectIO._materialize_embedded_assets(
                            full_data["assets"], resolver)
                        if embedded:
                            full_data["embedded_assets"] = embedded
                    # project.json 索引层素材路径同步回落（standalone
                    # 图片工程等不经 media_pool 的路径）。
                    if isinstance(project_data.get("assets"), dict):
                        ToposProjectIO._materialize_embedded_assets(
                            project_data["assets"], resolver)
                        project = ToposProject.from_dict(project_data)

                # edit/bins.json
                bins_path = os.path.join(cache_dir, "edit/bins.json")
                if os.path.exists(bins_path):
                    full_data["bins"] = ToposProjectIO._read_json(bins_path, config)

                # edit/timelines.json
                timelines_path = os.path.join(cache_dir, "edit/timelines.json")
                if os.path.exists(timelines_path):
                    full_data["timelines"] = ToposProjectIO._read_json(timelines_path, config)

                # edit/timeline_sequences.json
                timeline_sequences_path = os.path.join(cache_dir, "edit/timeline_sequences.json")
                if os.path.exists(timeline_sequences_path):
                    full_data["timeline_sequences"] = ToposProjectIO._read_json(timeline_sequences_path, config)

                # edit/comp_clips.json
                comp_clips_path = os.path.join(cache_dir, "edit/comp_clips.json")
                if os.path.exists(comp_clips_path):
                    full_data["comp_clips"] = ToposProjectIO._read_json(comp_clips_path, config)

                # edit/smart_bins.json
                smart_bins_path = os.path.join(cache_dir, "edit/smart_bins.json")
                if os.path.exists(smart_bins_path):
                    full_data["smart_bins"] = ToposProjectIO._read_json(smart_bins_path, config)

                # color/color_stacks.json
                color_stacks_path = os.path.join(cache_dir, "color/color_stacks.json")
                if os.path.exists(color_stacks_path):
                    full_data["color_stacks"] = ToposProjectIO._read_json(color_stacks_path, config)

                # composite/graphs.json
                graphs_path = os.path.join(cache_dir, "composite/graphs.json")
                if os.path.exists(graphs_path):
                    full_data["compositions"] = ToposProjectIO._read_json(graphs_path, config)

                # composite/viewer_state.json
                viewer_state_path = os.path.join(cache_dir, "composite/viewer_state.json")
                if os.path.exists(viewer_state_path):
                    full_data["composite_viewer_state"] = ToposProjectIO._read_json(viewer_state_path, config)

                # deliver/presets.json
                presets_path = os.path.join(cache_dir, "deliver/presets.json")
                if os.path.exists(presets_path):
                    full_data["deliver_presets"] = ToposProjectIO._read_json(presets_path, config)

                # ui/workspace_state.json
                workspace_path = os.path.join(cache_dir, "ui/workspace_state.json")
                if os.path.exists(workspace_path):
                    full_data["ui_state"] = ToposProjectIO._read_json(workspace_path, config)

                # AI 编辑子系统（TAEP）payload：会话目录 + plan 仓库。
                # 优先 v2 catalog，回退 v1 session.json；plan 仓库 ai/plans.json。
                ai_catalog_path = os.path.join(cache_dir, "ai/session_catalog.json")
                ai_session_path = os.path.join(cache_dir, "ai/session.json")
                if os.path.exists(ai_catalog_path):
                    full_data["ai_session_catalog"] = ToposProjectIO._read_json(ai_catalog_path, config)
                if os.path.exists(ai_session_path):
                    full_data["ai_session"] = ToposProjectIO._read_json(ai_session_path, config)
                ai_plans_path = os.path.join(cache_dir, "ai/plans.json")
                if os.path.exists(ai_plans_path):
                    full_data["ai_plans"] = ToposProjectIO._read_json(ai_plans_path, config)
                ai_jobs_path = os.path.join(cache_dir, "ai/jobs.json")
                if os.path.exists(ai_jobs_path):
                    full_data["ai_jobs"] = ToposProjectIO._read_json(ai_jobs_path, config)
                ai_analysis_path = os.path.join(cache_dir, "ai/analysis.json")
                if os.path.exists(ai_analysis_path):
                    full_data["ai_analysis"] = ToposProjectIO._read_json(ai_analysis_path, config)
                ai_runtime_path = os.path.join(cache_dir, "ai/runtime.json")
                if os.path.exists(ai_runtime_path):
                    full_data["ai_runtime"] = ToposProjectIO._read_json(ai_runtime_path, config)
                ai_trace_path = os.path.join(cache_dir, "ai/trace.json")
                if os.path.exists(ai_trace_path):
                    full_data["ai_trace"] = ToposProjectIO._read_json(ai_trace_path, config)
                ai_plugins_path = os.path.join(cache_dir, "ai/plugins.json")
                if os.path.exists(ai_plugins_path):
                    full_data["ai_plugins"] = ToposProjectIO._read_json(ai_plugins_path, config)

                teaching_animation_path = os.path.join(
                    cache_dir, "teaching_animation/scenes.json"
                )
                if os.path.exists(teaching_animation_path):
                    full_data["teaching_animation"] = ToposProjectIO._read_json(
                        teaching_animation_path, config
                    )
                teaching_animation_cache_path = os.path.join(
                    cache_dir, "teaching_animation/render_cache_manifest.json"
                )
                if os.path.exists(teaching_animation_cache_path):
                    full_data["teaching_animation_render_cache_manifest"] = (
                        ToposProjectIO._read_json(teaching_animation_cache_path, config)
                    )

                ai_workflows_path = os.path.join(
                    cache_dir, "ai_workflow/workflows.json"
                )
                if os.path.exists(ai_workflows_path):
                    full_data["ai_workflows"] = ToposProjectIO._read_json(
                        ai_workflows_path, config
                    )

                # cache_manifest.json
                cache_manifest_path = os.path.join(cache_dir, "cache_manifest.json")
                if os.path.exists(cache_manifest_path):
                    cache_manifest_data = ToposProjectIO._read_json(cache_manifest_path, config)
                    full_data["cache_manifest"] = CacheManifest.from_dict(cache_manifest_data)

                # 资产目录路径（持久 cache 目录）
                assets_dir = os.path.join(cache_dir, "assets")
                if os.path.exists(assets_dir):
                    full_data["assets_dir"] = assets_dir

                # A missing optional module must be observable to the host so
                # the UI can show an offline/recoverable state.  This remains
                # in shared project IO and does not import the feature module.
                if (
                    (
                        "teaching_animation" in manifest.present_modules
                        or "teaching_animation" in full_data
                    )
                    and not _is_teaching_animation_enabled()
                ):
                    full_data.setdefault("module_fallbacks", {})[
                        "teaching_animation"
                    ] = {
                        "module_id": "teaching_animation",
                        "state": "missing_module",
                        "payload_preserved": "teaching_animation" in full_data,
                        "cache_preserved": os.path.exists(
                            os.path.join(cache_dir, "assets")
                        ),
                    }

            # 4. 检查离线素材
            offline_assets = ToposProjectIO._check_offline_assets_in_project(project)
            if offline_assets:
                logger.warning(f"Found {len(offline_assets)} offline assets")
                full_data["offline_assets"] = offline_assets

            logger.info(f"Project loaded: {filepath}, assets={project.asset_count}, timelines={project.timeline_count}")
            return project, full_data

        except Exception as e:
            logger.error(f"Failed to load project: {e}")
            import traceback
            traceback.print_exc()
            return None, None
        finally:
            archive_token.release()

    @staticmethod
    def check_offline_assets(project: ToposProject) -> list[MediaAssetRef]:
        """
        检查工程中的离线素材。

        Args:
            project: ToposProject 实例

        Returns:
            离线素材列表（路径不存在的素材）
        """
        return ToposProjectIO._check_offline_assets_in_project(project)

    @staticmethod
    def check_offline_assets_with_report(
        project: ToposProject,
        project_path: str | None = None,
    ) -> "OfflineAssetReport":
        """
        检查工程中的离线素材，返回详细报告。

        使用 OfflineAssetManager 生成详细的离线状态报告，
        包含离线素材列表、缺失路径、建议搜索目录等信息。

        Args:
            project: ToposProject 实例
            project_path: 工程文件路径（可选，用于报告）

        Returns:
            OfflineAssetReport 离线素材报告
        """
        from .offline_asset_manager import OfflineAssetManager

        if project_path:
            return OfflineAssetManager.check_offline_assets_with_path(project, project_path)
        else:
            return OfflineAssetManager.check_offline_assets(project)

    @staticmethod
    def show_offline_dialog(
        project: ToposProject,
        parent: "QWidget | None" = None,
    ) -> dict[str, str] | None:
        """
        显示离线素材重链接对话框。

        如果存在离线素材，显示 RelinkMediaDialog 供用户重链接。
        返回用户选择的 relink_map。

        Args:
            project: ToposProject 实例
            parent: 父 QWidget（可选）

        Returns:
            relink_map: {asset_id: new_path} 映射，或 None 如果用户取消
        """
        from .offline_asset_manager import OfflineAssetManager

        offline_list = OfflineAssetManager.get_offline_asset_list(project)
        if not offline_list:
            return None

        # 使用 adapter 的重链接对话框（避免直接导入 features UI）
        from .edit_project_adapter import edit_project_adapter
        return edit_project_adapter.show_relink_dialog(offline_list, parent)

    @staticmethod
    def relink_assets(
        project: ToposProject,
        relink_map: dict[str, str],
    ) -> ToposProject:
        """
        重新链接素材。

        Args:
            project: ToposProject 实例
            relink_map: {asset_id: new_path} 映射

        Returns:
            新的 ToposProject 实例（更新后的素材路径）
        """
        new_assets = dict(project.assets)
        for asset_id, new_path in relink_map.items():
            if asset_id in new_assets:
                old_asset = new_assets[asset_id]
                new_assets[asset_id] = MediaAssetRef(
                    asset_id=old_asset.asset_id,
                    path=new_path,
                    media_type=old_asset.media_type,
                    display_name=old_asset.display_name,
                    is_offline=False,  # 重链接后设置为在线
                )

        return ToposProject(
            project_id=project.project_id,
            name=project.name,
            project_mode=project.project_mode,
            assets=new_assets,
            bins=project.bins,
            timelines=project.timelines,
            color_stacks=project.color_stacks,
            compositions=project.compositions,
            deliver_presets=project.deliver_presets,
            ui_state=project.ui_state,
            settings=project.settings,
            primary_asset_id=project.primary_asset_id,
            primary_color_stack_id=project.primary_color_stack_id,
            active_timeline_id=project.active_timeline_id,
            active_composition_id=project.active_composition_id,
            created_at=project.created_at,
            modified_at=time.time(),
        )

    # ==================== 内部方法 ====================

    @staticmethod
    def _write_json(filepath: str, data: dict[str, Any], config: ToposProjectIOConfig) -> None:
        """写入 JSON 文件"""
        if config.serialization_format == "msgpack":
            try:
                import msgpack
                packed = msgpack.packb(data, use_bin_type=True)
                if config.compress_project_file:
                    import gzip
                    with gzip.open(filepath + ".gz", 'wb', compresslevel=6) as f:
                        f.write(packed)
                else:
                    with open(filepath + ".mp", 'wb') as f:
                        f.write(packed)
            except ImportError:
                logger.warning("msgpack not available, falling back to JSON")
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
        else:
            if config.compress_project_file:
                import gzip
                with gzip.open(filepath + ".gz", 'wt', encoding='utf-8', compresslevel=6) as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            else:
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def _read_json(filepath: str, config: ToposProjectIOConfig) -> dict[str, Any]:
        """读取 JSON 文件（支持多种格式）"""
        # 检查是否存在 gzip 压缩文件
        gz_path = filepath + ".gz"
        mp_path = filepath + ".mp"
        mp_gz_path = filepath + ".mp.gz"

        if os.path.exists(mp_gz_path):
            import gzip
            try:
                import msgpack
                with gzip.open(mp_gz_path, 'rb') as f:
                    return msgpack.unpackb(f.read(), raw=False)
            except ImportError:
                logger.warning("msgpack not available, cannot read MessagePack format")

        if os.path.exists(mp_path):
            try:
                import msgpack
                with open(mp_path, 'rb') as f:
                    return msgpack.unpackb(f.read(), raw=False)
            except ImportError:
                logger.warning("msgpack not available, cannot read MessagePack format")

        if os.path.exists(gz_path):
            import gzip
            with gzip.open(gz_path, 'rt', encoding='utf-8') as f:
                return json.load(f)

        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)

        raise FileNotFoundError(f"No project data file found: {filepath}")

    @staticmethod
    def _materialize_embedded_assets(
        assets_data: dict[str, Any],
        resolver: ProjectResourceResolver,
    ) -> dict[str, str]:
        """DESK-01B：外部路径离线的素材回落到包内嵌资源。

        规则（顺序）：
        1. 外部路径在线 → 不动（内嵌资源只是补充，不是替代）；
        2. 素材带 ``resource_uri`` → 按内容身份解析（校验失败如实降级为
           离线并记入 resolver.diagnostics，不喂错内容给解码器）；
        3. 无 URI 但索引里有该 asset_id 的源资源条目 → 同上。

        返回 {asset_id: resource_uri}（被物化的素材）。物化路径位于解包
        缓存内——是本机实现细节，保存侧守卫保证不写回包（DESK-03C）。
        """
        embedded: dict[str, str] = {}
        for asset_id, asset_data in assets_data.items():
            if not isinstance(asset_data, dict):
                continue
            path = asset_data.get("path") or ""
            if path and not _is_package_cache_path(path) and os.path.exists(path):
                continue  # 外部在线
            resolved = None
            uri = asset_data.get("resource_uri")
            try:
                if uri:
                    resolved = resolver.resolve_uri(str(uri))
                else:
                    resolved = resolver.resolve_for_asset(asset_id)
            except ResourceResolutionError as exc:
                logger.warning(
                    "Embedded resource for asset %s failed to resolve; "
                    "asset stays offline: %s", asset_id, exc)
                continue
            if resolved is None:
                continue
            if path and not asset_data.get("path_original"):
                # 保留外部原路径提示（重链接/溯源用；authority 是 resource_uri）
                asset_data["path_original"] = path
            asset_data["path"] = resolved.local_path
            asset_data["resource_uri"] = resolved.entry.uri
            asset_data["is_offline"] = False
            embedded[asset_id] = resolved.entry.uri
        return embedded

    @staticmethod
    def _relativize_project_paths(project_data: dict[str, Any], base_dir: str) -> dict[str, Any]:
        """将 project.json 中的素材路径转为相对路径（缓存路径不落盘）"""
        assets = project_data.get("assets", {})
        for asset_id, asset_data in assets.items():
            if "path" not in asset_data:
                continue
            if _is_package_cache_path(asset_data["path"]):
                # DESK-03C：解包缓存路径不作为存储基准；保留既有
                # path_original（若有）供重链接，不写 path_relative。
                asset_data.pop("path_relative", None)
                continue
            asset_data["path_relative"] = ToposProjectIO._to_relative_path(asset_data["path"], base_dir)
            asset_data["path_original"] = asset_data["path"]
        return project_data

    @staticmethod
    def _absolutize_project_paths(project_data: dict[str, Any], base_dir: str) -> dict[str, Any]:
        """将 project.json 中的素材路径转为绝对路径"""
        assets = project_data.get("assets", {})
        for asset_id, asset_data in assets.items():
            # 优先使用相对路径转换
            if "path_relative" in asset_data:
                abs_path = ToposProjectIO._to_absolute_path(asset_data["path_relative"], base_dir)
                if os.path.exists(abs_path):
                    asset_data["path"] = abs_path
                elif "path_original" in asset_data and os.path.exists(asset_data["path_original"]):
                    asset_data["path"] = asset_data["path_original"]
            elif "path_original" in asset_data:
                if os.path.exists(asset_data["path_original"]):
                    asset_data["path"] = asset_data["path_original"]
        return project_data

    @staticmethod
    def _relativize_assets_paths(assets_data: dict[str, Any], base_dir: str) -> dict[str, Any]:
        """将 edit/media_pool.json 中的素材路径转为相对路径。

        DESK-03C：内嵌资源素材（resource_uri 为权威）当前 path 可能是
        解包缓存路径——缓存路径不落盘。落盘形状（契约）：
        ``path``/``path_original`` = 外部原路径提示（重链接用，可达性
        无要求），不写 ``path_relative``。
        """
        for asset_id, asset_data in assets_data.items():
            if not isinstance(asset_data, dict) or "path" not in asset_data:
                continue
            if asset_data.get("resource_uri"):
                external_hint = asset_data.get("path_original") or ""
                if not external_hint or _is_package_cache_path(external_hint):
                    external_hint = "" if _is_package_cache_path(
                        asset_data["path"]) else asset_data["path"]
                asset_data["path"] = external_hint
                asset_data["path_original"] = external_hint
                asset_data.pop("path_relative", None)
                continue
            if _is_package_cache_path(asset_data["path"]):
                # 无资源引用却持缓存路径（异常残留）：同样不落缓存路径
                asset_data.pop("path_relative", None)
                continue
            asset_data["path_relative"] = ToposProjectIO._to_relative_path(asset_data["path"], base_dir)
            asset_data["path_original"] = asset_data["path"]
        return assets_data

    @staticmethod
    def _absolutize_assets_paths(assets_data: dict[str, Any], base_dir: str) -> dict[str, Any]:
        """将 edit/media_pool.json 中的素材路径转为绝对路径"""
        for asset_id, asset_data in assets_data.items():
            if isinstance(asset_data, dict):
                if "path_relative" in asset_data:
                    abs_path = ToposProjectIO._to_absolute_path(asset_data["path_relative"], base_dir)
                    if os.path.exists(abs_path):
                        asset_data["path"] = abs_path
                    elif "path_original" in asset_data and os.path.exists(asset_data["path_original"]):
                        asset_data["path"] = asset_data["path_original"]
                elif "path_original" in asset_data:
                    if os.path.exists(asset_data["path_original"]):
                        asset_data["path"] = asset_data["path_original"]
        return assets_data

    @staticmethod
    def _to_relative_path(absolute_path: str, base_dir: str) -> str:
        """将绝对路径转换为相对路径"""
        try:
            abs_path = os.path.normpath(os.path.abspath(absolute_path))
            base = os.path.normpath(os.path.abspath(base_dir))
            rel_path = os.path.relpath(abs_path, base)
            # 如果相对路径以 .. 开头且超出工程目录，返回原始路径
            if rel_path.startswith(".."):
                return absolute_path
            return rel_path
        except Exception:
            return absolute_path

    @staticmethod
    def _to_absolute_path(relative_path: str, base_dir: str) -> str:
        """将相对路径转换为绝对路径"""
        try:
            if os.path.isabs(relative_path):
                return os.path.normpath(relative_path)
            abs_path = os.path.normpath(os.path.join(base_dir, relative_path))
            return abs_path
        except Exception:
            return relative_path

    @staticmethod
    def _check_offline_assets_in_project(project: ToposProject) -> list[MediaAssetRef]:
        """检查工程中的离线素材"""
        offline_assets = []
        for asset in project.assets.values():
            if not is_virtual_asset_path(asset.path) and not os.path.exists(asset.path):
                offline_assets.append(asset)
        return offline_assets

    @staticmethod
    def _copy_cache_assets(
        source_dir: str,
        dest_dir: str,
        max_size_mb: int,
    ) -> CacheManifest:
        """
        复制缓存资产到目标目录。

        Args:
            source_dir: 缓存资产源目录
            dest_dir: 目标目录（assets/）
            max_size_mb: 最大缓存大小（MB）

        Returns:
            CacheManifest 实例
        """
        cache_manifest = CacheManifest(created_at=time.time())
        total_size = 0
        max_size_bytes = max_size_mb * 1024 * 1024

        # 复制 baked 文件
        baked_src = os.path.join(source_dir, "baked")
        baked_dst = os.path.join(dest_dir, "assets/baked")
        if os.path.exists(baked_src):
            baked_files = {}
            for f in os.listdir(baked_src):
                src_path = os.path.join(baked_src, f)
                size = os.path.getsize(src_path)
                if total_size + size <= max_size_bytes:
                    shutil.copy2(src_path, os.path.join(baked_dst, f))
                    baked_files[f] = {"size": size}
                    total_size += size
            cache_manifest = CacheManifest(
                baked_files=baked_files,
                created_at=cache_manifest.created_at,
            )

        # 复制 AI 结果文件
        ai_src = os.path.join(source_dir, "ai_results")
        ai_dst = os.path.join(dest_dir, "assets/ai_results")
        if os.path.exists(ai_src):
            ai_files = {}
            for f in os.listdir(ai_src):
                src_path = os.path.join(ai_src, f)
                size = os.path.getsize(src_path)
                if total_size + size <= max_size_bytes:
                    shutil.copy2(src_path, os.path.join(ai_dst, f))
                    ai_files[f] = {"size": size}
                    total_size += size
            cache_manifest = CacheManifest(
                baked_files=cache_manifest.baked_files,
                ai_result_files=ai_files,
                created_at=cache_manifest.created_at,
            )

        # 复制缩略图
        thumb_src = os.path.join(source_dir, "thumbnails")
        thumb_dst = os.path.join(dest_dir, "assets/thumbnails")
        if os.path.exists(thumb_src):
            thumb_files = {}
            for f in os.listdir(thumb_src):
                src_path = os.path.join(thumb_src, f)
                size = os.path.getsize(src_path)
                if total_size + size <= max_size_bytes:
                    shutil.copy2(src_path, os.path.join(thumb_dst, f))
                    thumb_files[f] = {"size": size}
                    total_size += size
            cache_manifest = CacheManifest(
                baked_files=cache_manifest.baked_files,
                ai_result_files=cache_manifest.ai_result_files,
                thumbnail_files=thumb_files,
                created_at=cache_manifest.created_at,
            )

        # 复制代理文件
        proxy_src = os.path.join(source_dir, "proxies")
        proxy_dst = os.path.join(dest_dir, "assets/proxies")
        if os.path.exists(proxy_src):
            proxy_files = {}
            for f in os.listdir(proxy_src):
                src_path = os.path.join(proxy_src, f)
                size = os.path.getsize(src_path)
                if total_size + size <= max_size_bytes:
                    shutil.copy2(src_path, os.path.join(proxy_dst, f))
                    proxy_files[f] = {"size": size}
                    total_size += size
            cache_manifest = CacheManifest(
                baked_files=cache_manifest.baked_files,
                ai_result_files=cache_manifest.ai_result_files,
                thumbnail_files=cache_manifest.thumbnail_files,
                proxy_files=proxy_files,
                total_size_bytes=total_size,
                created_at=cache_manifest.created_at,
            )

        return cache_manifest
