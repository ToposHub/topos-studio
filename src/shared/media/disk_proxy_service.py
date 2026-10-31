"""
Disk Proxy Service Module
磁盘代理服务，统一管理代理生成、状态更新和播放整合。

## 职责

- DiskProxyService: 磁盘代理生命周期管理服务
- 代理生成任务调度
- 状态更新与回调
- 播放服务整合

## 设计原则

- 统一的代理管理入口
- 后台任务调度，不阻塞 UI
- 完整的状态追踪和回调
- 与 VideoFrameProviderService 整合
- 完整类型注解

## 使用示例

```python
from src.shared.media.disk_proxy_service import DiskProxyService, get_disk_proxy_service
from src.shared.media.proxy import DiskProxyConfig, DiskProxyInfo

# 获取服务实例
service = get_disk_proxy_service()

# 检查代理状态
status = service.get_proxy_status('/path/to/video.mp4')

# 触发代理生成
service.generate_proxy(
    source_path='/path/to/video.mp4',
    asset_id='asset-001',
    on_complete=lambda info: print(f"Done: {info.proxy_path}"),
)

# 获取代理信息（用于打开剪辑）
info = service.get_disk_proxy_info('/path/to/video.mp4')
clip_id = frame_provider.open_clip_with_disk_proxy('/path/to/video.mp4', info)
```

## 架构位置

```text
MediaPool Panel / VideoFrameProviderService
    └── DiskProxyService
            ├── ProxyManager（任务调度）
            ├── ProxyInfoCache（状态缓存）
            └── CallbackDispatcher（状态更新回调）
```
"""

from __future__ import annotations

import logging
import threading
from typing import (
    Optional,
    Callable,
    Dict,
    List,
    Any,
    Tuple,
)

from .proxy import (
    DiskProxyConfig,
    DiskProxyInfo,
    ProxyStatus,
    ProxyFormat,
    get_source_file_info,
    DEFAULT_DISK_PROXY_CONFIG,
)
from .proxy_manager import ProxyManager
from .proxy_generator import ProxyProgress, ProxyGenerationResult

logger = logging.getLogger(__name__)


# =============================================================================
# Asset -> DiskProxyInfo 转换函数
# =============================================================================

def asset_proxy_to_disk_proxy_info(asset: Any) -> Optional[DiskProxyInfo]:
    """
    从 MediaAsset 的 proxy 属性转换为 DiskProxyInfo。

    用于将 asset 层的 proxy_path/proxy_status（供 UI 显示）转换为
    播放层需要的 DiskProxyInfo 结构。

    Args:
        asset: 项目素材对象，需要有 proxy_path/proxy_status 属性

    Returns:
        DiskProxyInfo 如果 proxy 存在且有效，否则 None

    Note:
        - proxy_status 必须为 'ready' 才返回有效 DiskProxyInfo
        - proxy_resolution 从 asset.metadata 推断（原始分辨率的一半）
        - proxy_format 默认为 PRORES_PROXY
        - source_mtime/source_size 需要实际文件检查（此处设为 0）

    Examples:
        >>> asset = MediaAsset(...)
        >>> asset.proxy_status = 'ready'
        >>> asset.proxy_path = '/path/to/proxy.mov'
        >>> info = asset_proxy_to_disk_proxy_info(asset)
        >>> info.status
        ProxyStatus.READY
    """
    if not asset or not hasattr(asset, 'proxy_path'):
        return None

    if not asset.proxy_path or asset.proxy_status != 'ready':
        return None

    # 尝试从 metadata 获取分辨率
    resolution: Optional[Tuple[int, int]] = None
    metadata = getattr(asset, 'metadata', {})
    width = metadata.get('width')
    height = metadata.get('height')
    if width and height:
        # 代理分辨率通常是原始的一半（保持偶数）
        resolution = (width // 2 // 2 * 2, height // 2 // 2 * 2)

    # 尝试获取代理格式（如果 asset 有该属性）
    proxy_format: Optional[ProxyFormat] = None
    if hasattr(asset, 'proxy_format'):
        proxy_format = getattr(asset, 'proxy_format')
    else:
        # 默认 ProRes Proxy
        proxy_format = ProxyFormat.PRORES_PROXY

    return DiskProxyInfo(
        status=ProxyStatus.READY,
        proxy_path=asset.proxy_path,
        proxy_resolution=resolution,
        proxy_format=proxy_format,
        source_mtime=0.0,
        source_size=0,
    )


# =============================================================================
# 回调类型
# =============================================================================

# 代理状态变化回调
ProxyStatusCallback = Callable[[str, DiskProxyInfo], None]

# 代理进度回调
ProxyProgressCallback = Callable[[str, ProxyProgress], None]


# =============================================================================
# DiskProxyService
# =============================================================================

class DiskProxyService:
    """
    磁盘代理服务。

    统一管理磁盘代理的生成、状态追踪和播放整合。

    职责：
    - 代理生成任务调度（通过 ProxyManager）
    - 代理状态缓存（source_path -> DiskProxyInfo）
    - 状态变化回调分发（通知 UI 和播放服务）
    - 自动触发播放服务更新

    Attributes:
        _config: 代理配置
        _manager: ProxyManager 实例
        _info_cache: source_path -> DiskProxyInfo 缓存
        _status_callbacks: 状态变化回调列表
        _progress_callbacks: 进度回调列表
        _lock: 线程锁

    Examples:
        >>> service = DiskProxyService()
        >>> info = service.get_disk_proxy_info('/path/to/video.mp4')
        >>> info.status
        ProxyStatus.MISSING
    """

    def __init__(
        self,
        config: Optional[DiskProxyConfig] = None,
        max_concurrent_tasks: int = 2,
    ) -> None:
        """
        初始化磁盘代理服务。

        Args:
            config: 磁盘代理配置（默认 ProRes 422 1/2）
            max_concurrent_tasks: 最大并发生成任务数
        """
        self._config: DiskProxyConfig = config or DEFAULT_DISK_PROXY_CONFIG
        self._manager: ProxyManager = ProxyManager(
            config=self._config,
            max_concurrent=max_concurrent_tasks,
        )
        self._info_cache: Dict[str, DiskProxyInfo] = {}
        self._status_callbacks: List[ProxyStatusCallback] = []
        self._progress_callbacks: List[ProxyProgressCallback] = []
        # AUD-34509：按 source_path 挂起的调用方回调注册表——对生成中的
        # 文件再次 generate_proxy 时，on_complete/on_error/on_progress 挂到
        # 对应源上，终态（完成/失败/取消）消费并清理；不再追加进全局列表
        # （全局列表无移除路径，泄漏闭包且 _dispatch_progress_callbacks 不
        # 按 path 过滤会跨文件串扰）。
        self._pending_caller_callbacks: Dict[
            str, List[Tuple[Optional[ProxyStatusCallback], Optional[Callable[[str, str], None]]]]
        ] = {}
        self._pending_progress_callbacks: Dict[str, List[ProxyProgressCallback]] = {}
        self._lock: threading.RLock = threading.RLock()

        # 统计
        self._total_generated: int = 0
        self._total_failed: int = 0
        self._total_bytes_saved: int = 0  # 代理文件总大小

        logger.info(
            f"DiskProxyService initialized: format={config.format.name if config else 'ProRes'}, "
            f"resolution={config.resolution.name if config else 'HALF'}, "
            f"max_concurrent={max_concurrent_tasks}"
        )

    # =============================================================================
    # 代理状态查询
    # =============================================================================

    def get_proxy_status(self, source_path: str) -> ProxyStatus:
        """
        获取代理状态。

        Args:
            source_path: 源文件路径

        Returns:
            ProxyStatus 枚举值
        """
        with self._lock:
            info = self._info_cache.get(source_path)
            if info is None:
                return ProxyStatus.MISSING
            return info.status

    def get_disk_proxy_info(self, source_path: str) -> DiskProxyInfo:
        """
        获取磁盘代理信息。

        验证代理状态，返回最新的 DiskProxyInfo。

        Args:
            source_path: 源文件路径

        Returns:
            DiskProxyInfo 实例

        Note:
            - 如果代理文件存在且有效，返回 READY 状态
            - 如果代理过期（源文件修改），返回 STALE 状态
            - 如果代理不存在，返回 MISSING 状态
        """
        with self._lock:
            info = self._info_cache.get(source_path)

            # AUD-34507：缓存命中复验——READY 条目比对源文件签名
            # （mtime/size），源文件在外部被修改/替换后降级 STALE。
            # 此前一旦进入缓存即无条件原样返回，docstring 承诺的 STALE
            # 检测从未生效。指纹缺失（mtime/size 为 0 的外部播报条目）
            # 时跳过复验（无从比对）。
            if info is not None and info.is_ready and (
                    info.source_mtime != 0.0 or info.source_size != 0):
                try:
                    mtime, size = get_source_file_info(source_path)
                except OSError as e:
                    logger.warning(f"Failed to revalidate source info: {e}")
                else:
                    if mtime != info.source_mtime or size != info.source_size:
                        logger.info(
                            f"Proxy stale (source modified): {source_path}"
                        )
                        info = info.with_status(ProxyStatus.STALE)
                        self._info_cache[source_path] = info

            # 如果没有缓存信息，检查代理是否已存在
            if info is None:
                # 尝试从 ProxyManager 获取
                proxy_path = self._manager.get_valid_proxy_path(source_path)
                if proxy_path is not None:
                    # 代理存在且有效
                    try:
                        mtime, size = get_source_file_info(source_path)

                        # AUD-34508：从源文件元数据推导代理分辨率；
                        # 探测失败置 None（此前硬编码 4K 基准推算，非 4K
                        # 源得到错误分辨率，如 1080p 源被记为 1920x1080）
                        proxy_res = self._discover_proxy_resolution(source_path)

                        info = DiskProxyInfo(
                            status=ProxyStatus.READY,
                            proxy_path=proxy_path,
                            proxy_resolution=proxy_res,
                            proxy_format=self._config.format,
                            source_mtime=mtime,
                            source_size=size,
                        )
                        self._info_cache[source_path] = info
                        # AUD-34514：磁盘发现路径不再累计 _total_bytes_saved
                        # ——该口径只统计本服务实际生成的代理字节数（与
                        # cleanup_all_proxies 清零后重复发现会重复累计的
                        # 旧口径冲突），"发现已有代理"由 manager_stats.
                        # total_proxy_size_bytes 反映

                        logger.debug(
                            f"Proxy found: {source_path} -> {proxy_path}"
                        )

                    except OSError as e:
                        logger.warning(f"Failed to get proxy info: {e}")
                        info = DiskProxyInfo(status=ProxyStatus.MISSING)
                        self._info_cache[source_path] = info

                else:
                    # 没有代理
                    info = DiskProxyInfo(status=ProxyStatus.MISSING)
                    self._info_cache[source_path] = info

            return info

    def _discover_proxy_resolution(
        self,
        source_path: str,
    ) -> Optional[Tuple[int, int]]:
        """
        探测源文件分辨率并按当前配置推导代理分辨率（AUD-34508）。

        Args:
            source_path: 源文件路径

        Returns:
            代理分辨率 (width, height)；探测失败或源分辨率未知时
            返回 None（不虚构值——下游据此保持"未知"语义）
        """
        try:
            from .factory import MediaSourceFactory
            result = MediaSourceFactory.probe_video_metadata_cached(source_path)
            metadata = getattr(result, "metadata", None)
            width = int(getattr(metadata, "width", 0) or 0)
            height = int(getattr(metadata, "height", 0) or 0)
        except Exception as e:  # noqa: BLE001 — 探测失败按未知分辨率处理
            logger.debug(
                f"Proxy resolution probe failed for {source_path}: {e}"
            )
            return None
        if width <= 0 or height <= 0:
            return None
        try:
            return self._config.resolution.calculate_size((width, height))
        except Exception as e:  # noqa: BLE001 — 配置计算异常按未知处理
            logger.debug(
                f"Proxy resolution calculation failed for {source_path}: {e}"
            )
            return None

    def has_ready_proxy(self, source_path: str) -> bool:
        """
        检查是否有就绪的代理。

        Args:
            source_path: 源文件路径

        Returns:
            True 表示代理就绪
        """
        info = self.get_disk_proxy_info(source_path)
        return info.is_ready

    def mark_ready(self, source_path: str, info: DiskProxyInfo) -> None:
        """
        Publish externally-produced proxy info into the service cache.

        AUD-20203 服务侧公共 cache 写入 API：素材面板等调用方在自有
        管线产出代理后，用本方法把（通常为 READY 的）DiskProxyInfo 播报
        进服务——调用方不再持服务的私有锁直写 ``_info_cache``。写入后与
        内部生成完成路径一致地分发状态回调（订阅方如
        AssetProxyManager 会据此同步 manifest）。

        Args:
            source_path: 源文件路径
            info: 外部产出的代理信息

        Note:
            - 不触发 VideoFrameProviderService 更新；需要时由调用方通过
              其公共接口自行完成。
            - 传入 GENERATING 之外的状态同样被接受（本方法只是公共写入
              口，不猜调用方意图）。
        """
        with self._lock:
            self._info_cache[source_path] = info
        logger.debug(
            f"DiskProxyService cache marked: {source_path} -> {info.proxy_path}"
        )
        self._dispatch_status_callbacks(source_path, info)

    # =============================================================================
    # 代理生成
    # =============================================================================

    def generate_proxy(
        self,
        source_path: str,
        asset_id: str,
        on_complete: Optional[ProxyStatusCallback] = None,
        on_progress: Optional[ProxyProgressCallback] = None,
        on_error: Optional[Callable[[str, str], None]] = None,
    ) -> Optional[str]:
        """
        触发代理生成。

        如果代理已就绪，直接返回。否则启动后台生成任务。

        Args:
            source_path: 源文件路径
            asset_id: 素材 ID
            on_complete: 完成回调（接收 source_path 和 DiskProxyInfo）
            on_progress: 进度回调（接收 source_path 和 ProxyProgress）
            on_error: 错误回调（接收 source_path 和 error_message）

        Returns:
            任务 ID，如果代理已就绪则返回 None

        Note:
            - 代理生成在后台线程执行
            - 完成后自动更新 _info_cache
            - 完成后自动调用 on_complete 回调
            - 完成后自动触发 VideoFrameProviderService 更新（如果已打开）
        """
        # 检查是否已有就绪的代理
        info = self.get_disk_proxy_info(source_path)
        if info.is_ready:
            logger.info(f"Proxy already ready: {source_path}")
            if on_complete:
                on_complete(source_path, info)
            return None

        # 检查是否正在生成
        if info.status == ProxyStatus.GENERATING:
            logger.info(f"Proxy already generating: {source_path}")
            # AUD-34509：调用方回调挂到该 source_path 的注册表，终态
            # （完成/失败/取消）由 _on_generation_complete/_on_generation_error
            # 消费并清理。此前 on_progress 被追加进全局列表（永不移除：
            # 泄漏 + 跨文件串扰），on_complete/on_error 被静默丢弃。
            self._attach_pending_caller_callbacks(
                source_path, on_complete, on_progress, on_error)
            return None

        # 更新状态为 GENERATING
        # AUD-34510：置位前在锁内复验缓存，关闭"检查-置位"之间的 TOCTOU
        # 窗口——并发调用已抢先置位 GENERATING / 完成置 READY 时不得覆盖，
        # 落败方按既有分支语义处理（挂回调 / 直接交付）。
        with self._lock:
            cached = self._info_cache.get(source_path)
            if cached is not None and cached is not info and (
                    cached.is_ready or cached.status == ProxyStatus.GENERATING):
                info = cached
            else:
                self._info_cache[source_path] = info.with_status(
                    ProxyStatus.GENERATING)
                info = None  # 置位成功，由本调用负责入队

        if info is not None:
            # 竞态落败：状态已被其他线程改写
            if info.is_ready:
                logger.info(f"Proxy already ready (raced): {source_path}")
                if on_complete:
                    on_complete(source_path, info)
                return None
            logger.info(f"Proxy already generating (raced): {source_path}")
            self._attach_pending_caller_callbacks(
                source_path, on_complete, on_progress, on_error)
            return None

        # 启动生成任务
        task_id = self._manager.queue_proxy_generation(
            asset_id=asset_id,
            source_path=source_path,
            on_complete=lambda result: self._on_generation_complete(
                source_path, result, on_complete
            ),
            on_error=lambda error_msg: self._on_generation_error(
                source_path, error_msg, on_error
            ),
            on_progress=lambda progress: self._on_generation_progress(
                source_path, progress, on_progress
            ),
        )

        if task_id:
            logger.info(f"Proxy generation started: {source_path}, task={task_id}")

        return task_id

    def _attach_pending_caller_callbacks(
        self,
        source_path: str,
        on_complete: Optional[ProxyStatusCallback],
        on_progress: Optional[ProxyProgressCallback],
        on_error: Optional[Callable[[str, str], None]],
    ) -> None:
        """
        把调用方回调挂到在途生成的注册表（AUD-34509/34510，锁内）。

        终态（完成/失败/取消）由 _on_generation_complete/
        _on_generation_error 消费并清理。
        """
        with self._lock:
            if on_progress is not None:
                self._pending_progress_callbacks.setdefault(
                    source_path, []).append(on_progress)
            if on_complete is not None or on_error is not None:
                self._pending_caller_callbacks.setdefault(
                    source_path, []).append((on_complete, on_error))

    def pause_all_generation(self) -> int:
        """
        挂起所有后台代理生成，为前台交互任务让出 CPU。

        SIGSTOP 运行中的 ffmpeg 进程（保留已编码进度），并推迟排队任务
        启动。场景剪切探测等前台任务运行期间调用，结束后必须调用
        resume_all_generation() 恢复。

        Returns:
            被挂起的运行中任务数量
        """
        return self._manager.pause_all_generation()

    def resume_all_generation(self) -> None:
        """恢复被 pause_all_generation() 挂起/延迟的后台代理生成。"""
        self._manager.resume_all_generation()

    def cancel_generation(self, source_path: str) -> bool:
        """
        取消代理生成。

        Args:
            source_path: 源文件路径

        Returns:
            True 表示成功取消
        """
        with self._lock:
            info = self._info_cache.get(source_path)
            if info is None or info.status != ProxyStatus.GENERATING:
                return False

            # 按源文件路径查找任务（任务创建时以 source_path 入队，
            # asset_id 可能与调用方持有的标识不一致）
            task = self._manager.get_task_by_source_path(source_path)
            if task is None:
                return False

            # 取消任务
            cancelled = self._manager.cancel_task(task.task_id)
            if cancelled:
                # 更新状态
                info = info.with_status(ProxyStatus.MISSING)
                self._info_cache[source_path] = info
                logger.info(f"Proxy generation cancelled: {source_path}")

        # AUD-34310/34509：取消是终态——任务被取消后不会再触发
        # on_complete 回调，这里同步消费挂起调用方回调并广播状态，
        # 避免 manager manifest 滞留 GENERATING / pending 永不清理。
        # （锁外调用，遵循"锁内拷贝、锁外调用"的分发模式）
        if cancelled:
            self._notify_cancelled(source_path, info)

        return cancelled

    def cancel_all_generations(self) -> int:
        """
        取消所有生成任务。

        Returns:
            取消的任务数
        """
        cancelled = self._manager.cancel_all_tasks()

        # 更新所有 GENERATING 状态为 MISSING
        with self._lock:
            cancelled_infos: List[Tuple[str, DiskProxyInfo]] = []
            for source_path, info in self._info_cache.items():
                if info.status == ProxyStatus.GENERATING:
                    info = info.with_status(ProxyStatus.MISSING)
                    self._info_cache[source_path] = info
                    cancelled_infos.append((source_path, info))

        for source_path, info in cancelled_infos:
            self._notify_cancelled(source_path, info)

        logger.info(f"Cancelled {cancelled} proxy generation tasks")
        return cancelled

    # =============================================================================
    # 状态更新回调
    # =============================================================================

    def register_status_callback(
        self,
        callback: ProxyStatusCallback,
    ) -> None:
        """
        注册状态变化回调。

        Args:
            callback: 回调函数（source_path, DiskProxyInfo）
        """
        with self._lock:
            self._status_callbacks.append(callback)

    def unregister_status_callback(
        self,
        callback: ProxyStatusCallback,
    ) -> None:
        """
        移除状态变化回调。

        Args:
            callback: 回调函数
        """
        with self._lock:
            self._status_callbacks.remove(callback)

    def register_progress_callback(
        self,
        callback: ProxyProgressCallback,
    ) -> None:
        """
        注册进度回调。

        Args:
            callback: 回调函数（source_path, ProxyProgress）
        """
        with self._lock:
            self._progress_callbacks.append(callback)

    # =============================================================================
    # 代理管理
    # =============================================================================

    def cleanup_expired_proxies(self) -> int:
        """
        清理过期代理文件。

        Returns:
            清理的文件数
        """
        cleaned = self._manager.cleanup_expired_proxies()

        # 更新缓存状态
        with self._lock:
            for source_path, info in self._info_cache.items():
                if info.status == ProxyStatus.STALE:
                    self._info_cache[source_path] = info.with_status(ProxyStatus.MISSING)

        logger.info(f"Cleaned up {cleaned} expired proxies")
        return cleaned

    def cleanup_all_proxies(self) -> int:
        """
        清理所有代理文件。

        Returns:
            清理的文件数
        """
        cleaned = self._manager.cleanup_all_proxies()

        # 清空缓存
        with self._lock:
            self._info_cache.clear()
            self._total_bytes_saved = 0

        logger.info(f"Cleaned up all {cleaned} proxies")
        return cleaned

    def get_proxy_stats(self) -> Dict[str, Any]:
        """
        获取代理统计信息。

        Returns:
            统计信息字典
        """
        with self._lock:
            manager_stats = self._manager.get_stats()

            # 计算状态分布
            status_counts = {
                ProxyStatus.MISSING: 0,
                ProxyStatus.GENERATING: 0,
                ProxyStatus.READY: 0,
                ProxyStatus.STALE: 0,
                ProxyStatus.FAILED: 0,
            }
            for info in self._info_cache.values():
                status_counts[info.status] += 1

            return {
                'manager_stats': {
                    'assets_with_proxy': manager_stats.assets_with_proxy,
                    'pending_tasks': manager_stats.pending_tasks,
                    'generating_tasks': manager_stats.generating_tasks,
                    'failed_tasks': manager_stats.failed_tasks,
                    'total_proxy_size_bytes': manager_stats.total_proxy_size_bytes,
                },
                # AUD-34514：计数读取整体置于临界区内，避免与后台完成
                # 回调的并发写入产生不一致快照
                'service_stats': {
                    'total_generated': self._total_generated,
                    'total_failed': self._total_failed,
                    'total_bytes_saved': self._total_bytes_saved,
                },
                'status_distribution': {
                    status.name: count for status, count in status_counts.items()
                },
                'cache_size': len(self._info_cache),
            }

    # =============================================================================
    # 内部回调处理
    # =============================================================================

    def _on_generation_complete(
        self,
        source_path: str,
        result: ProxyGenerationResult,
        user_callback: Optional[ProxyStatusCallback],
    ) -> None:
        """
        处理生成完成。

        Args:
            source_path: 源文件路径
            result: 生成结果
            user_callback: 用户回调
        """
        if result.success:
            # 更新缓存
            with self._lock:
                try:
                    mtime, size = get_source_file_info(source_path)
                except OSError:
                    mtime, size = 0.0, 0

                info = DiskProxyInfo(
                    status=ProxyStatus.READY,
                    proxy_path=result.proxy_path,
                    proxy_resolution=result.proxy_resolution,
                    proxy_format=self._config.format,
                    source_mtime=mtime,
                    source_size=size,
                )
                self._info_cache[source_path] = info
                self._total_generated += 1
                self._total_bytes_saved += result.proxy_size_bytes

            logger.info(
                f"Proxy generated: {source_path} -> {result.proxy_path}, "
                f"{result.elapsed_seconds:.1f}s, "
                f"{result.proxy_size_bytes / 1024 / 1024:.1f}MB"
            )

            # 调用用户回调
            if user_callback:
                user_callback(source_path, info)

            # AUD-34509：消费挂起调用方回调（生成期间重复 generate_proxy
            # 注册的 on_complete），消费后清理注册表
            for pending_complete, _pending_error in self._drain_pending_callers(source_path):
                if pending_complete is not None:
                    try:
                        pending_complete(source_path, info)
                    except Exception as e:  # noqa: BLE001 — 回调异常不阻断分发
                        logger.warning(f"Pending complete callback error: {e}")

            # 分发状态变化回调
            self._dispatch_status_callbacks(source_path, info)

            # 更新 VideoFrameProviderService（如果已打开）
            self._update_frame_provider_service(source_path, info)

        elif result.cancelled:
            # 用户取消
            with self._lock:
                info = self._info_cache.get(source_path)
                if info is not None:
                    info = info.with_status(ProxyStatus.MISSING)
                    self._info_cache[source_path] = info
                else:
                    info = DiskProxyInfo(status=ProxyStatus.MISSING)

            logger.info(f"Proxy generation cancelled: {source_path}")

            # AUD-34310：取消是终态——广播 MISSING 状态（订阅方如
            # AssetProxyManager 据此把 manifest 从 GENERATING 同步出来），
            # 并消费挂起调用方回调
            self._notify_cancelled(source_path, info)

        else:
            # 失败
            self._on_generation_error(source_path, result.error_message, None)

    def _notify_cancelled(
        self,
        source_path: str,
        info: DiskProxyInfo,
    ) -> None:
        """
        取消终态统一收口（AUD-34310/34509）：消费挂起回调 + 广播状态。

        Args:
            source_path: 源文件路径
            info: 取消后的（MISSING）代理信息
        """
        for pending_complete, _pending_error in self._drain_pending_callers(source_path):
            if pending_complete is not None:
                try:
                    pending_complete(source_path, info)
                except Exception as e:  # noqa: BLE001 — 回调异常不阻断分发
                    logger.warning(f"Pending cancel callback error: {e}")
        self._dispatch_status_callbacks(source_path, info)

    def _drain_pending_callers(
        self,
        source_path: str,
    ) -> List[Tuple[Optional[ProxyStatusCallback], Optional[Callable[[str, str], None]]]]:
        """
        取出并清理该源挂起的调用方回调（含挂起的进度回调）。

        Args:
            source_path: 源文件路径

        Returns:
            挂起的 (on_complete, on_error) 列表（可能为空）
        """
        with self._lock:
            callers = list(self._pending_caller_callbacks.pop(source_path, []))
            self._pending_progress_callbacks.pop(source_path, None)
        return callers

    def _copy_pending_progress(
        self,
        source_path: str,
    ) -> List[ProxyProgressCallback]:
        """
        拷贝该源挂起的进度回调（不清理——进度会多次触发）。

        Args:
            source_path: 源文件路径

        Returns:
            进度回调列表（可能为空）
        """
        with self._lock:
            return list(self._pending_progress_callbacks.get(source_path, []))

    def _on_generation_error(
        self,
        source_path: str,
        error_message: str,
        user_callback: Optional[Callable[[str, str], None]],
    ) -> None:
        """
        处理生成失败。

        Args:
            source_path: 源文件路径
            error_message: 错误消息
            user_callback: 用户回调
        """
        with self._lock:
            info = self._info_cache.get(source_path)
            if info is not None:
                info = DiskProxyInfo(
                    status=ProxyStatus.FAILED,
                    error_message=error_message,
                )
                self._info_cache[source_path] = info
            self._total_failed += 1

        logger.error(f"Proxy generation failed: {source_path}, error={error_message}")

        # 调用用户回调
        if user_callback:
            user_callback(source_path, error_message)

        # AUD-34509：消费挂起调用方的 on_error（生成期间重复
        # generate_proxy 注册的回调此前被静默丢弃）
        for _pending_complete, pending_error in self._drain_pending_callers(source_path):
            if pending_error is not None:
                try:
                    pending_error(source_path, error_message)
                except Exception as e:  # noqa: BLE001 — 回调异常不阻断分发
                    logger.warning(f"Pending error callback error: {e}")

        # AUD-34310：失败终态同样广播状态（订阅方据此同步 manifest）
        if info is not None:
            self._dispatch_status_callbacks(source_path, info)

    def _on_generation_progress(
        self,
        source_path: str,
        progress: ProxyProgress,
        user_callback: Optional[ProxyProgressCallback],
    ) -> None:
        """
        处理生成进度。

        Args:
            source_path: 源文件路径
            progress: 进度信息
            user_callback: 用户回调
        """
        # 更新缓存中的进度
        with self._lock:
            info = self._info_cache.get(source_path)
            if info is not None:
                self._info_cache[source_path] = info.with_progress(progress.percent)

        # 调用用户回调
        if user_callback:
            user_callback(source_path, progress)

        # AUD-34509：按 source_path 分发给挂起的进度回调（不跨文件串扰）
        for pending_progress in self._copy_pending_progress(source_path):
            try:
                pending_progress(source_path, progress)
            except Exception as e:  # noqa: BLE001 — 回调异常不阻断分发
                logger.warning(f"Pending progress callback error: {e}")

        # 分发进度回调
        self._dispatch_progress_callbacks(source_path, progress)

    def _dispatch_status_callbacks(
        self,
        source_path: str,
        info: DiskProxyInfo,
    ) -> None:
        """
        分发状态变化回调。

        Args:
            source_path: 源文件路径
            info: 代理信息
        """
        with self._lock:
            callbacks = list(self._status_callbacks)

        for callback in callbacks:
            try:
                callback(source_path, info)
            except Exception as e:
                logger.warning(f"Status callback error: {e}")

    def _dispatch_progress_callbacks(
        self,
        source_path: str,
        progress: ProxyProgress,
    ) -> None:
        """
        分发进度回调。

        Args:
            source_path: 源文件路径
            progress: 进度信息
        """
        with self._lock:
            callbacks = list(self._progress_callbacks)

        for callback in callbacks:
            try:
                callback(source_path, progress)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    def _update_frame_provider_service(
        self,
        source_path: str,
        info: DiskProxyInfo,
    ) -> None:
        """
        更新 VideoFrameProviderService 的代理信息。

        Args:
            source_path: 源文件路径
            info: 新的代理信息

        Note:
            如果剪辑已打开，更新代理信息会触发重新打开磁盘代理源。
        """
        # 尝试获取 VideoFrameProviderService
        try:
            from src.shared.video.frame_provider_service import get_frame_provider_service

            service = get_frame_provider_service()

            # 查找已打开的剪辑
            with service._lock:
                clip_ids = service._path_to_clips.get(source_path, [])
                if clip_ids:
                    clip_id = clip_ids[0]
                    session = service._clips.get(clip_id)
                    if session and session.is_open:
                        # 更新代理信息
                        service.update_disk_proxy_info(clip_id, info)

                        logger.info(
                            f"Updated frame provider with new proxy: "
                            f"{source_path} -> {info.proxy_path}"
                        )

        except Exception as e:
            logger.warning(f"Failed to update frame provider: {e}")

    # =============================================================================
    # 配置更新
    # =============================================================================

    def update_config(self, config: DiskProxyConfig) -> None:
        """
        更新代理配置。

        Args:
            config: 新的配置

        Note:
            更新配置后，现有代理可能变为 STALE（因为配置变化）。
        """
        with self._lock:
            self._config = config
            self._manager.config = config

            # AUD-34507：配置参与代理 key（格式/分辨率变化后旧代理可能
            # 不再匹配）——清空 READY 缓存，让后续查询重新从磁盘发现并
            # 按新配置复验/重建；生成中的任务完成后会经回调重新写入。
            self._info_cache.clear()

        logger.info(f"Proxy config updated: format={config.format}, resolution={config.resolution}")

    @property
    def config(self) -> DiskProxyConfig:
        """获取当前配置"""
        return self._config


# =============================================================================
# 全局服务实例
# =============================================================================

_GLOBAL_SERVICE: Optional[DiskProxyService] = None
_SERVICE_LOCK: threading.Lock = threading.Lock()


def _proxy_format_from_config() -> ProxyFormat:
    """偏好设置 `proxy_format` 键 → ProxyFormat；缺省 topos_proxy。

    topos-first 策略（2026-09-19 审计）：渲染缓存默认已是 topos Proxy，
    素材代理补齐同款默认。topos 源无论此设置恒走 topos（proxy_generator
    内容嗅探强制）；本键只决定**普通源**的代理编码。topos 代理为纯视频
    容器（ADR-C008）——回放音频始终取 master，不经代理，无行为回归。
    """
    value = ''
    try:
        from src.shared.config.config_manager import ConfigManager
        value = str(ConfigManager().get('proxy_format') or '').strip().lower()
    except Exception:  # noqa: BLE001 — 配置层缺失时用默认，不阻断代理服务
        value = ''
    mapping = {
        'topos_proxy': ProxyFormat.TOPOS_PROXY,
        'topos': ProxyFormat.TOPOS_PROXY,
        'prores_proxy': ProxyFormat.PRORES_PROXY,
        'prores': ProxyFormat.PRORES_PROXY,
        'h264': ProxyFormat.H264,
    }
    fmt = mapping.get(value)
    if fmt is None:
        if value:
            logger.warning(
                "Unknown proxy_format %r in preferences; falling back to "
                "topos_proxy", value)
        return ProxyFormat.TOPOS_PROXY
    return fmt


def get_disk_proxy_service() -> DiskProxyService:
    """
    获取全局磁盘代理服务实例。

    代理编码格式读偏好设置 `proxy_format` 键（首次创建时生效；改动偏好
    后经 reset_disk_proxy_service() 或重启生效——与渲染缓存目录键同款
    生命周期）。

    Returns:
        DiskProxyService 实例
    """
    global _GLOBAL_SERVICE

    with _SERVICE_LOCK:
        if _GLOBAL_SERVICE is None:
            _GLOBAL_SERVICE = DiskProxyService(
                config=DiskProxyConfig(
                    format=_proxy_format_from_config(),
                ),
            )
        return _GLOBAL_SERVICE


def reset_disk_proxy_service() -> None:
    """
    重置全局磁盘代理服务。

    用于测试或应用重置。
    """
    global _GLOBAL_SERVICE

    with _SERVICE_LOCK:
        if _GLOBAL_SERVICE is not None:
            _GLOBAL_SERVICE.cancel_all_generations()
            _GLOBAL_SERVICE = None