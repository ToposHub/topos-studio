"""批 A（Critical）shared/video 播放链路修复回归测试。

覆盖三个审查发现的修复（全部使用 fake 源/会话构造分支逻辑，不做真实解码）：

- AUD-39003: FallbackLogger.export_to_json 持非重入 _events_lock 调用
  get_statistics（内部二次加锁）的确定性死锁
- AUD-39100: _close_clip_sources / update_disk_proxy_info 释放 CPU 源
  （master/proxy/disk_proxy）必须持 session.decode_lock，与在途
  _decode_frame 的 read_frame 串行化（use-after-close 竞态）
- AUD-39305: playback_clock.SourceManager.open() READY 磁盘代理路径缺
  ``import os`` 导致 NameError，open() 整体失败
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.media.metadata import Rational, VideoMetadata
from src.shared.media.proxy import (
    DiskProxyInfo,
    ProxyStatus,
    get_source_file_info,
)
from src.shared.video.fallback_types import (
    FallbackEvent,
    VideoPipelineFallbackReason,
    fallback_logger,
)
from src.shared.video.frame_provider_service import (
    ClipSession,
    VideoFrameProviderService,
)
from src.shared.video.playback_clock import SourceManager

# 死锁/竞态判定用等待上限（秒）：正常路径毫秒级完成，死锁则挂满超时。
JOIN_TIMEOUT_S = 3.0


def _run_in_thread(target: Any, *args: Any) -> threading.Thread:
    """在守护线程中执行 target，异常不吞栈（记录到 thread 属性）。"""

    captured: List[BaseException] = []

    def _wrapper() -> None:
        try:
            target(*args)
        except BaseException as exc:  # noqa: BLE001 - 测试需完整捕获
            captured.append(exc)

    thread = threading.Thread(target=_wrapper, daemon=True)
    thread.captured = captured  # type: ignore[attr-defined]
    thread.start()
    return thread


# ============================================================================
# AUD-39003: export_to_json 持非重入锁二次加锁死锁
# ============================================================================


def _make_event(codec: Optional[str]) -> FallbackEvent:
    return FallbackEvent(
        reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
        codec=codec,
        pixel_format="yuv422p10le",
        backend="ffmpeg",
        source_type="master_only",
        frame_path="planar_yuv_gpu",
    )


def test_export_to_json_does_not_deadlock() -> None:
    """export_to_json 必须在锁外调用 get_statistics（锁内只取快照）。"""
    fallback_logger.clear()
    try:
        for codec in ("prores", "h264", "hevc"):
            fallback_logger.log(_make_event(codec))

        thread = _run_in_thread(fallback_logger.export_to_json)
        thread.join(timeout=JOIN_TIMEOUT_S)
        assert not thread.is_alive(), (
            "export_to_json 死锁：持非重入 _events_lock 调用 get_statistics "
            "触发同线程二次加锁"
        )
        assert not thread.captured, f"export_to_json 抛异常: {thread.captured}"
    finally:
        fallback_logger.clear()


def test_export_to_json_output_shape() -> None:
    """修复后导出内容仍包含事件与统计。"""
    fallback_logger.clear()
    try:
        fallback_logger.log(_make_event("prores"))
        fallback_logger.log(_make_event("prores"))

        payload = fallback_logger.export_to_json()
        data = json.loads(payload)
        assert len(data["events"]) == 2
        stats = data["statistics"]
        assert stats["total_events"] == 2
        assert stats["reason_counts"].get("v3_init_failed") == 2
        assert stats["codec_counts"].get("prores") == 2
    finally:
        fallback_logger.clear()


# ============================================================================
# AUD-39100: CPU 源 close 必须持 decode_lock（与在途解码串行化）
# ============================================================================


class _RecordingSource:
    """记录 close 调用顺序的最小 MediaSource 桩。"""

    def __init__(self, name: str, closed: List[str]) -> None:
        self.name = name
        self._closed = closed

    def close(self) -> None:
        self._closed.append(self.name)


def test_close_clip_sources_waits_for_decode_lock() -> None:
    """decode_lock 被在途解码持有时，_close_clip_sources 不得 close 任何 CPU 源。"""
    service = VideoFrameProviderService()
    closed: List[str] = []
    session = ClipSession(clip_id="aud39100-close", path="/mock/a.mov")
    session.is_open = True
    session.master_source = _RecordingSource("master", closed)
    session.proxy_source = _RecordingSource("proxy", closed)
    session.disk_proxy_source = _RecordingSource("disk_proxy", closed)

    # 模拟在途解码：主线程持有 decode_lock（read_frame 进行中）
    assert session.decode_lock.acquire(timeout=JOIN_TIMEOUT_S)
    try:
        closer = _run_in_thread(service._close_clip_sources, session)
        closer.join(timeout=1.0)
        assert closer.is_alive(), (
            "_close_clip_sources 必须阻塞等待 decode_lock，而不是锁外并发 close"
        )
        assert closed == [], "在途解码持锁期间不得提前释放源（use-after-close）"
    finally:
        session.decode_lock.release()

    closer.join(timeout=JOIN_TIMEOUT_S)
    assert not closer.captured, f"_close_clip_sources 抛异常: {closer.captured}"
    assert closed == ["master", "proxy", "disk_proxy"]
    assert session.master_source is None
    assert session.proxy_source is None
    assert session.disk_proxy_source is None
    assert session.is_open is False


def test_update_disk_proxy_info_stale_close_waits_for_decode_lock() -> None:
    """STALE 分支关闭磁盘代理源同样必须持 decode_lock。"""
    service = VideoFrameProviderService()
    closed: List[str] = []
    session = ClipSession(clip_id="aud39100-stale", path="/mock/b.mov")
    session.is_open = True
    session.metadata = None  # metadata 为 None 时不会触发内存代理重建
    session.disk_proxy_source = _RecordingSource("disk_proxy", closed)
    service._clips[session.clip_id] = session

    stale_info = DiskProxyInfo(
        status=ProxyStatus.STALE,
        proxy_path="/mock/proxy.mov",
        proxy_resolution=(960, 540),
    )

    assert session.decode_lock.acquire(timeout=JOIN_TIMEOUT_S)
    try:
        updater = _run_in_thread(
            service.update_disk_proxy_info, session.clip_id, stale_info
        )
        updater.join(timeout=1.0)
        assert updater.is_alive(), (
            "update_disk_proxy_info 关闭磁盘代理源必须等待 decode_lock"
        )
        assert closed == [], "在途解码持锁期间不得提前 close 磁盘代理源"
    finally:
        session.decode_lock.release()

    updater.join(timeout=JOIN_TIMEOUT_S)
    assert not updater.captured, f"update_disk_proxy_info 抛异常: {updater.captured}"
    assert closed == ["disk_proxy"]
    assert session.disk_proxy_source is None


# ============================================================================
# AUD-39305: READY 磁盘代理路径缺 import os → NameError → open() 失败
# ============================================================================


class _StubMediaSource:
    """SourceManager.open() 所需的最小 MediaSource 桩。"""

    def __init__(self, metadata: VideoMetadata) -> None:
        self._metadata = metadata

    def open(self) -> bool:
        return True

    def close(self) -> None:
        return None

    def probe(self) -> VideoMetadata:
        return self._metadata

    def set_preserve_planes(self, value: bool) -> None:
        return None


def test_playback_clock_module_imports_os() -> None:
    """模块必须导入 os（:366 的 os.path.isfile 依赖）。"""
    import src.shared.video.playback_clock as playback_clock_module

    assert hasattr(playback_clock_module, "os")


def test_source_manager_open_with_ready_disk_proxy(tmp_path: Path) -> None:
    """READY 磁盘代理路径不得因 NameError 使 SourceManager.open() 整体失败。"""
    source_file = tmp_path / "source.mov"
    source_file.write_bytes(b"source-bytes")
    proxy_file = tmp_path / "proxy.mov"
    proxy_file.write_bytes(b"proxy-bytes")

    # mtime/size 必须与源文件当前值一致，validate_disk_proxy 才保持 READY
    source_mtime, source_size = get_source_file_info(str(source_file))

    master_metadata = VideoMetadata(
        width=1920,
        height=1080,
        fps=Rational(24, 1),
        total_frames=120,
        codec_name="h264",
        codec_profile="Main",
        pixel_format="yuv420p",
        bit_depth=8,
    )
    proxy_metadata = VideoMetadata(
        width=960,
        height=540,
        fps=Rational(24, 1),
        total_frames=120,
        codec_name="prores",
        codec_profile="HQ",
        pixel_format="yuv422p10le",
        bit_depth=10,
    )
    proxy_path_str = str(proxy_file)

    def factory(path: str, *args: Any, **kwargs: Any) -> _StubMediaSource:
        if path == proxy_path_str:
            return _StubMediaSource(proxy_metadata)
        return _StubMediaSource(master_metadata)

    ready_info = DiskProxyInfo(
        status=ProxyStatus.READY,
        proxy_path=proxy_path_str,
        proxy_resolution=(960, 540),
        source_mtime=source_mtime,
        source_size=source_size,
    )

    manager = SourceManager(
        path=str(source_file),
        source_factory=factory,
        disk_proxy_info=ready_info,
    )
    try:
        # 修复前：READY 分支 os.path.isfile → NameError → open() 返回 False
        assert manager.open() is True, (
            "READY 磁盘代理路径不得导致 SourceManager.open() 整体失败"
        )
        assert manager.is_open is True
        assert manager._disk_proxy_source is not None, (
            "校验通过的 READY 磁盘代理应当被打开并挂载"
        )
    finally:
        manager.close()
