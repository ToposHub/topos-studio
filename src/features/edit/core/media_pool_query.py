"""媒体池大数据查询（P0-4）——可取消、分页、稳定 item_id 的后端查询。

为十万素材级媒体池提供不阻塞 UI 的查询通道：

- **引擎**（``execute_media_pool_query``）：纯函数，收 ``EditProject`` 与
  ``MediaPoolQuery``，返回 ``MediaPoolQueryResult``（稳定 asset_id 序、
  总数、工程 revision）。过滤/排序语义与媒体池面板同步路径一致
  （``_apply_search_filter`` / ``_pool_sort_value`` 同款口径：未知值垫底、
  名称稳定次序），保证大小池行为一致。
- **异步会话**（``MediaPoolQuerySession``）：QRunnable worker + 代际
  （generation）守卫。快速输入只接收最新请求（旧结果按 query_id 丢弃），
  工程切换/修订变化后的迟到结果一律拒绝。UI 只读结果，不改工程。

搜索域：当前 Bin / All Bins；字段：All Fields / File Name / Codec /
Resolution / FPS。排序键：name/type/resolution/duration/date/fps/audio/
scene/codec/rating/color_label/flag。
"""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from src.features.edit.core.project import EditProject, MediaAsset

__all__ = [
    "MEDIA_POOL_QUERY_CANCEL_INTERVAL",
    "MediaPoolQueryCancelled",
    "MediaPoolQuery",
    "MediaPoolQueryResult",
    "execute_media_pool_query",
    "MediaPoolQueryWorker",
    "MediaPoolQuerySession",
]

# 协作式取消检查间隔（素材数）：100k 池约 25 次检查点。
MEDIA_POOL_QUERY_CANCEL_INTERVAL = 4096

# 搜索字段域（与面板 SearchFilterMode 同口径）
FIELD_ALL = "all_fields"
FIELD_FILENAME = "filename"
FIELD_CODEC = "codec"
FIELD_RESOLUTION = "resolution"
FIELD_FPS = "fps"
VALID_FIELDS = (FIELD_ALL, FIELD_FILENAME, FIELD_CODEC, FIELD_RESOLUTION, FIELD_FPS)

# 排序键（与面板 _pool_sort_value / SORT_BY_* 同口径）
VALID_SORT_KEYS = (
    "name", "type", "resolution", "duration", "date", "fps",
    "audio", "scene", "codec", "rating", "color_label", "flag", "path",
)

_SCOPE_BIN = "bin"
_SCOPE_UNFILED = "unfiled"
_SCOPE_ALL_BINS = "all_bins"
VALID_SCOPES = (_SCOPE_BIN, _SCOPE_UNFILED, _SCOPE_ALL_BINS)


class MediaPoolQueryCancelled(Exception):
    """查询被协作取消（引擎在检查点抛出，由执行器转成 cancelled 结果）。"""


def _valid_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass(frozen=True)
class MediaPoolQuery:
    """一次媒体池查询请求（值对象，query_id 是请求身份）。"""

    query_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    text: str = ""
    field: str = FIELD_FILENAME
    scope: str = _SCOPE_ALL_BINS
    bin_id: Optional[str] = None      # scope=bin 时必填
    sort_key: str = "name"
    sort_desc: bool = False
    offset: int = 0
    limit: Optional[int] = None       # None = 全量（结果仍受排序成本约束）

    def __post_init__(self) -> None:
        if self.field not in VALID_FIELDS:
            raise ValueError(f"未知搜索字段: {self.field}")
        if self.scope not in VALID_SCOPES:
            raise ValueError(f"未知查询范围: {self.scope}")
        if self.scope == _SCOPE_BIN and not self.bin_id:
            raise ValueError("scope=bin 需要 bin_id")
        if self.sort_key not in VALID_SORT_KEYS:
            raise ValueError(f"未知排序键: {self.sort_key}")
        if self.offset < 0:
            raise ValueError("offset 不能为负")
        if self.limit is not None and self.limit <= 0:
            raise ValueError("limit 必须为正")


@dataclass(frozen=True)
class MediaPoolQueryResult:
    """查询结果：稳定 asset_id 有序元组 + 总数 + 工程 revision。

    ``asset_ids`` 已按排序键排好序（未知值垫底）；``total`` 是过滤后的
    总匹配数（分页前），供 UI 显示与滚动估算。``cancelled=True`` 时
    ``asset_ids`` 为空且结果必须被丢弃。
    """

    query_id: str
    project_id: str
    revision: int
    total: int
    asset_ids: tuple[str, ...]
    offset: int
    cancelled: bool = False
    elapsed_ms: float = 0.0


# ================================================================
# 过滤（与面板 _apply_search_filter 同口径）
# ================================================================

def _all_fields_text(asset: MediaAsset) -> str:
    return " ".join(str(value or "") for value in (
        asset.display_name, asset.path, asset.reel, asset.scene,
        asset.shot, asset.take, asset.codec, asset.resolution_str,
        asset.fps, asset.keywords, asset.comment, asset.color_label,
    )).casefold()


def _matches(asset: MediaAsset, text: str, query_field: str) -> bool:
    if not text:
        return True
    needle = text.casefold()
    if query_field == FIELD_ALL:
        return needle in _all_fields_text(asset)
    if query_field == FIELD_FILENAME:
        return needle in (asset.display_name or "").casefold()
    if query_field == FIELD_CODEC:
        return needle in (asset.codec or "").casefold()
    if query_field == FIELD_RESOLUTION:
        res_str = (asset.resolution_str or "").casefold()
        width_height = f"{_valid_int(asset.width) or 0}x{_valid_int(asset.height) or 0}"
        return needle in res_str or needle in width_height
    if query_field == FIELD_FPS:
        if asset.fps is None:
            return False
        return needle in f"{asset.fps:.2f}" or needle in str(asset.fps)
    return True


# ================================================================
# 排序（与面板 _pool_sort_value 同口径；未知值垫底、名称稳定次序）
# ================================================================

def _sort_value(asset: MediaAsset, sort_key: str) -> Any:
    if sort_key == "name":
        return (asset.display_name or "").casefold()
    if sort_key == "type":
        return asset.media_type.casefold()
    if sort_key == "resolution":
        width, height = _valid_int(asset.width), _valid_int(asset.height)
        if width and height and width > 0 and height > 0:
            return width * height
        return None
    if sort_key == "date":
        value = asset.created_at
        return value if isinstance(value, (int, float)) and math.isfinite(value) else None
    if sort_key == "duration":
        value = asset.duration_seconds
        return value if isinstance(value, (int, float)) and math.isfinite(value) else None
    if sort_key == "fps":
        value = asset.fps
        return value if isinstance(value, (int, float)) and math.isfinite(value) else None
    if sort_key == "audio":
        channels = asset.audio_channels
        return channels if isinstance(channels, int) else None
    if sort_key == "scene":
        value = asset.production_info_str
        return value.casefold() if value and value.strip() else None
    if sort_key == "codec":
        value = asset.codec
        return value.casefold() if value else None
    if sort_key == "rating":
        return asset.rating
    if sort_key == "color_label":
        return asset.color_label or None
    if sort_key == "flag":
        return asset.flag or None
    if sort_key == "path":
        return asset.path.casefold() if asset.path else None
    return None


def _ordered_asset_ids(
    assets: list[MediaAsset], sort_key: str, sort_desc: bool,
) -> list[str]:
    known: list[tuple[Any, str, str]] = []  # (value, namefold, asset_id)
    unknown: list[tuple[str, str]] = []     # (namefold, asset_id)
    for asset in assets:
        value = _sort_value(asset, sort_key)
        namefold = (asset.display_name or "").casefold()
        if value is None or (sort_key not in ("name", "type", "rating")
                             and isinstance(value, str) and not value.strip()):
            unknown.append((namefold, asset.asset_id))
        else:
            known.append((value, namefold, asset.asset_id))
    known.sort(key=lambda t: (t[0], t[1]), reverse=sort_desc)
    unknown.sort()
    return [asset_id for _, _, asset_id in known] + [
        asset_id for _, asset_id in unknown]


# ================================================================
# 引擎
# ================================================================

def _scope_assets(project: EditProject, query: MediaPoolQuery) -> list[MediaAsset]:
    if query.scope == _SCOPE_ALL_BINS:
        return list(project.assets.values())
    if query.scope == _SCOPE_BIN:
        bin_obj = project.bins.get(query.bin_id or "")
        if bin_obj is None:
            raise ValueError(f"Bin not found: {query.bin_id}")
        assets = [project.assets[aid] for aid in bin_obj.asset_ids
                  if aid in project.assets]
        return assets
    # unfiled（Master 层级）
    return [project.assets[aid] for aid in project.get_unfiled_asset_ids()
            if aid in project.assets]


def execute_media_pool_query(
    project: EditProject,
    query: MediaPoolQuery,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> MediaPoolQueryResult:
    """执行查询（纯函数；可在任意线程运行，工程不可变共享安全）。

    Args:
        project: 工程快照（frozen，读线程安全）
        query: 查询请求
        cancel_check: 协作取消回调（返回 True 即取消）；每
            ``MEDIA_POOL_QUERY_CANCEL_INTERVAL`` 个素材检查一次

    Returns:
        结果；取消时 ``cancelled=True`` 且 asset_ids 为空
    """
    started = time.perf_counter()
    try:
        scope_assets = _scope_assets(project, query)
        filtered: list[MediaAsset] = []
        for index, asset in enumerate(scope_assets):
            if cancel_check is not None and index % MEDIA_POOL_QUERY_CANCEL_INTERVAL == 0:
                if cancel_check():
                    raise MediaPoolQueryCancelled()
            if _matches(asset, query.text, query.field):
                filtered.append(asset)
        total = len(filtered)
        ordered = _ordered_asset_ids(filtered, query.sort_key, query.sort_desc)
        end = query.offset + (query.limit if query.limit is not None else total)
        page = ordered[query.offset:end]
        return MediaPoolQueryResult(
            query_id=query.query_id,
            project_id=project.project_id,
            revision=project.revision,
            total=total,
            asset_ids=tuple(page),
            offset=query.offset,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
    except MediaPoolQueryCancelled:
        return MediaPoolQueryResult(
            query_id=query.query_id,
            project_id=project.project_id,
            revision=project.revision,
            total=0,
            asset_ids=(),
            offset=query.offset,
            cancelled=True,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )


# ================================================================
# 异步 worker + 会话（latest-wins / 工程身份守卫）
# ================================================================

class _QuerySignals(QObject):
    """QRunnable 不能自带 Qt 信号，经独立 signals 对象发射。"""

    finished = pyqtSignal(object)   # MediaPoolQueryResult
    failed = pyqtSignal(str, str)   # query_id, error


class MediaPoolQueryWorker(QRunnable):
    """后台执行一次查询（不持有 UI 引用；结果经信号回主线程）。"""

    def __init__(self, project: EditProject, query: MediaPoolQuery) -> None:
        super().__init__()
        self._project = project
        self._query = query
        self._cancelled = False
        self.signals = _QuerySignals()

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # QThreadPool 工作线程
        try:
            result = execute_media_pool_query(
                self._project, self._query,
                cancel_check=lambda: self._cancelled,
            )
        except Exception as exc:  # noqa: BLE001 — 单个查询失败不拖垮会话
            self.signals.failed.emit(self._query.query_id, str(exc))
            return
        self.signals.finished.emit(result)


class MediaPoolQuerySession(QObject):
    """媒体池查询会话：提交最新查询、拒绝过期结果。

    - ``submit`` 每次递增 generation 并取消在途 worker（协作取消）；
      只有 query_id 等于**最新一代**的结果会经 ``results_ready`` 发出。
    - 工程身份守卫：submit 时记录 project_id + revision；结果携带的
      project_id/revision 与提交时不一致（工程已被命令修改或切换）即
      丢弃，由下一次刷新重新发起。
    - UI 侧永远只消费 ``results_ready``，不直接持有 worker。
    """

    results_ready = pyqtSignal(object)      # MediaPoolQueryResult（最新）
    query_failed = pyqtSignal(str, str)     # query_id, error（最新）

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)  # 查询排队串行，天然 latest-wins
        self._generation = 0
        self._latest_query_id: str = ""
        self._expected_project_id: str = ""
        self._expected_revision: int = -1
        self._active_worker: Optional[MediaPoolQueryWorker] = None

    # -- 生命周期 -------------------------------------------------

    def shutdown(self) -> None:
        """取消在途查询并等待线程退出（面板关闭时调用）。"""
        self._generation += 1
        self._latest_query_id = ""
        if self._active_worker is not None:
            self._active_worker.cancel()
            self._active_worker = None
        self._pool.clear()
        self._pool.waitForDone(200)

    # -- 提交与守卫 ------------------------------------------------

    def submit(self, project: EditProject, query: MediaPoolQuery) -> str:
        """提交查询；返回被接受时的 generation。"""
        self._generation += 1
        self._latest_query_id = query.query_id
        self._expected_project_id = project.project_id
        self._expected_revision = project.revision
        if self._active_worker is not None:
            self._active_worker.cancel()  # 协作取消在途查询
            self._active_worker = None
        worker = MediaPoolQueryWorker(project, query)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.failed.connect(self._on_failed)
        self._active_worker = worker
        self._pool.start(worker)
        return str(self._generation)

    def _is_current(self, query_id: str) -> bool:
        return bool(query_id) and query_id == self._latest_query_id

    def _on_finished(self, result: Any) -> None:
        if not self._is_current(getattr(result, "query_id", "")):
            return
        if getattr(result, "cancelled", False):
            return  # 被更新请求取消的结果不上抛
        if (getattr(result, "project_id", "") != self._expected_project_id
                or getattr(result, "revision", -1) != self._expected_revision):
            return  # 工程已变更：迟到结果拒绝，等待下一次刷新
        self.results_ready.emit(result)

    def _on_failed(self, query_id: str, error: str) -> None:
        if self._is_current(query_id):
            self.query_failed.emit(query_id, error)
