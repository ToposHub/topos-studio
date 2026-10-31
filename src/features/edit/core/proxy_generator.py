"""
Proxy Generator Module - Generate low-resolution proxy files.

P6.3 Offline/Relink/Proxy functionality.

Generates proxy files for video and image assets using FFmpeg.
Proxy files are stored in the same directory as the original file
with a "_proxy" suffix.

Usage:
    from src.features.edit.core.proxy_generator import ProxyGeneratorWorker

    # Synchronous generation
    proxy_path = ProxyGeneratorWorker.generate_proxy(asset)

    # Or use as QRunnable for background execution
    worker = ProxyGeneratorWorker(asset)
    worker.signals.progress.connect(on_progress)
    worker.signals.finished.connect(on_finished)
    worker.signals.error.connect(on_error)
    QThreadPool.globalInstance().start(worker)
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal, QRunnable

from src.features.edit.core.project import MediaAsset
from src.shared.ffmpeg_locator import find_ffmpeg

logger = logging.getLogger(__name__)


# ================================================================
# Proxy Generation Signals
# ================================================================

class ProxySignals(QObject):
    """Signals for proxy generation worker."""

    progress = pyqtSignal(str, int)  # (asset_id, percentage)
    finished = pyqtSignal(str, str)  # (asset_id, proxy_path)
    error = pyqtSignal(str, str)     # (asset_id, error_message)


# ================================================================
# Proxy Generation Configuration
# ================================================================

@dataclass(frozen=True)
class ProxyConfig:
    """
    Proxy generation configuration.

    Attributes:
        video_width: Target width for video proxies (720p)
        video_codec: Video codec for proxies
        video_crf: Constant Rate Factor (quality)
        video_preset: Encoding speed preset
        audio_codec: Audio codec for proxies
        audio_bitrate: Audio bitrate
        image_max_size: Maximum dimension for image proxies
        image_format: Output format for image proxies
        suffix: Proxy file name suffix
    """

    # Video proxy settings (720p, H.264, medium quality)
    video_width: int = 1280
    video_codec: str = "libx264"
    video_crf: int = 23  # Medium quality (18-28 range)
    video_preset: str = "medium"  # Encoding speed

    # Audio proxy settings
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"

    # Image proxy settings (1920px max dimension)
    image_max_size: int = 1920
    image_format: str = "jpg"  # JPEG for smaller file size
    image_quality: int = 85  # JPEG quality (1-100)

    # File naming
    suffix: str = "_proxy"


# Default configuration
DEFAULT_PROXY_CONFIG = ProxyConfig()


# ================================================================
# Proxy Generator Worker
# ================================================================

class ProxyGeneratorWorker(QRunnable):
    """
    QRunnable worker for proxy generation.

    Generates low-resolution proxy files for video and image assets
    using FFmpeg. Runs in a background thread via QThreadPool.

    Usage:
        worker = ProxyGeneratorWorker(asset)
        worker.signals.progress.connect(on_progress)
        worker.signals.finished.connect(on_finished)
        QThreadPool.globalInstance().start(worker)
    """

    def __init__(
        self,
        asset: MediaAsset,
        config: Optional[ProxyConfig] = None,
        parent: Optional[QObject] = None
    ) -> None:
        """
        Initialize proxy generator worker.

        Args:
            asset: MediaAsset to generate proxy for
            config: Proxy generation configuration
            parent: Parent QObject
        """
        super().__init__()
        self.asset = asset
        self.config = config or DEFAULT_PROXY_CONFIG
        self.signals = ProxySignals()
        self._cancelled = False

    def run(self) -> None:
        """Execute proxy generation in background thread."""
        try:
            self.signals.progress.emit(self.asset.asset_id, 0)

            proxy_path = self.generate_proxy(
                self.asset,
                self.config,
                lambda p: self.signals.progress.emit(self.asset.asset_id, p),
                cancelled_fn=lambda: self._cancelled,
            )

            if proxy_path:
                self.signals.finished.emit(self.asset.asset_id, proxy_path)
            else:
                self.signals.error.emit(
                    self.asset.asset_id,
                    "Proxy generation returned no path"
                )

        except Exception as e:
            logger.error(f"Proxy generation failed for {self.asset.path}: {e}")
            self.signals.error.emit(self.asset.asset_id, str(e))

    @staticmethod
    def generate_proxy(
        asset: MediaAsset,
        config: Optional[ProxyConfig] = None,
        progress_callback: Optional[Any] = None,
        cancelled_fn: Optional[Callable[[], bool]] = None
    ) -> Optional[str]:
        """
        Generate proxy file for an asset.

        Args:
            asset: MediaAsset to generate proxy for
            config: Proxy generation configuration
            progress_callback: Optional callback for progress updates
            cancelled_fn: Optional cancellation check polled during encoding

        Returns:
            Proxy file path, or None if generation failed
        """
        config = config or DEFAULT_PROXY_CONFIG

        # Check if asset exists
        if not os.path.exists(asset.path):
            logger.warning(f"Asset file does not exist: {asset.path}")
            return None

        # Generate proxy path
        proxy_path = _generate_proxy_path(asset.path, config.suffix)

        try:
            if asset.is_video:
                return _generate_video_proxy(
                    asset, proxy_path, config, progress_callback,
                    cancelled_fn=cancelled_fn,
                )
            elif asset.is_image:
                return _generate_image_proxy(asset, proxy_path, config, progress_callback)
            else:
                logger.warning(f"Proxy generation not supported for media type: {asset.media_type}")
                return None

        except Exception as e:
            logger.error(f"Failed to generate proxy for {asset.path}: {e}")
            # Clean up partial file
            if os.path.exists(proxy_path):
                try:
                    os.remove(proxy_path)
                except OSError:
                    pass
            return None


# ================================================================
# Video Proxy Generation
# ================================================================

def _generate_video_proxy(
    asset: MediaAsset,
    proxy_path: str,
    config: ProxyConfig,
    progress_callback: Optional[Any] = None,
    cancelled_fn: Optional[Callable[[], bool]] = None
) -> Optional[str]:
    """
    Generate video proxy（PyAV 主路径，ffmpeg CLI 兜底）。

    Creates a 720p-width H.264 proxy with AAC audio. PyAV uses the FFmpeg
    libraries bundled with the app, so packaged builds work without a
    system ffmpeg installation; the CLI path only serves as a fallback.

    Args:
        asset: Source video asset
        proxy_path: Output proxy file path
        config: Proxy configuration
        progress_callback: Progress callback
        cancelled_fn: Optional cancellation check invoked per decoded frame

    Returns:
        Proxy file path, or None if failed
    """
    logger.info(f"Generating video proxy: {asset.path} -> {proxy_path}")

    proxy = _generate_video_proxy_pyav(
        asset, proxy_path, config, progress_callback, cancelled_fn,
    )
    if proxy is not None:
        return proxy

    if cancelled_fn is not None and cancelled_fn():
        return None

    logger.debug("PyAV proxy unavailable; falling back to ffmpeg CLI")
    return _generate_video_proxy_cli(asset, proxy_path, config, progress_callback)


def _generate_video_proxy_pyav(
    asset: MediaAsset,
    proxy_path: str,
    config: ProxyConfig,
    progress_callback: Optional[Any] = None,
    cancelled_fn: Optional[Callable[[], bool]] = None
) -> Optional[str]:
    """PyAV 进程内编码（无需系统 ffmpeg），失败返回 None 交由 CLI 兜底。"""
    try:
        import av

        cancelled = False
        input_container = av.open(asset.path)
        try:
            input_stream = input_container.streams.video[0]
            src_w = int(input_stream.codec_context.width)
            src_h = int(input_stream.codec_context.height)
            if src_w <= 0 or src_h <= 0:
                return None
            rate = input_stream.average_rate or input_stream.base_rate
            if rate is None:
                return None
            # AUD-17233：进度按总时长归一化。旧实现 frame.time*100 在
            # 任何 >1 秒的视频上立即被 min(95,…) 截到 95%，进度条全程失真。
            total_duration_seconds = _container_duration_seconds(
                input_container, input_stream,
            )

            out_w = config.video_width
            out_h = max(2, int(src_h * out_w / src_w) // 2 * 2)

            muxer_options = (
                {"movflags": "+faststart"}
                if proxy_path.lower().endswith((".mp4", ".mov")) else {}
            )
            output_container = av.open(proxy_path, "w", options=muxer_options)
            try:
                output_stream = output_container.add_stream(
                    config.video_codec, rate=rate,
                )
                output_stream.width, output_stream.height = out_w, out_h
                output_stream.pix_fmt = "yuv420p"
                output_stream.codec_context.options = {
                    "crf": str(config.video_crf),
                    "preset": config.video_preset,
                }

                audio_stream = (
                    input_container.streams.audio[0]
                    if input_container.streams.audio else None
                )
                out_audio_stream = None
                audio_resampler = None
                if audio_stream is not None:
                    try:
                        audio_resampler = av.AudioResampler(
                            format="fltp",
                            layout=audio_stream.layout or "stereo",
                            rate=int(audio_stream.rate or 48000),
                        )
                        out_audio_stream = output_container.add_stream(
                            config.audio_codec,
                            rate=int(audio_stream.rate or 48000),
                        )
                        out_audio_stream.bit_rate = 128000
                    except Exception as audio_exc:
                        logger.warning(
                            "Proxy audio encode unavailable (%s); "
                            "continuing video-only", audio_exc,
                        )
                        audio_resampler = None
                        out_audio_stream = None

                streams = (
                    [input_stream] if audio_stream is None
                    or out_audio_stream is None
                    else [input_stream, audio_stream]
                )
                for packet in input_container.demux(*streams):
                    if cancelled_fn is not None and cancelled_fn():
                        cancelled = True
                        break
                    if packet.stream.type == "audio":
                        if out_audio_stream is None:
                            continue
                        for decoded_audio in packet.decode():
                            for resampled in _resample_audio(
                                audio_resampler, decoded_audio,
                            ):
                                for p in out_audio_stream.encode(resampled):
                                    output_container.mux(p)
                        continue
                    if packet.stream.type != "video":
                        continue
                    for decoded_frame in packet.decode():
                        proxy_frame = decoded_frame.reformat(
                            width=out_w, height=out_h, format="yuv420p",
                        )
                        for p in output_stream.encode(proxy_frame):
                            output_container.mux(p)
                        if progress_callback:
                            progress_callback(_encode_progress_percent(
                                proxy_frame.time, total_duration_seconds,
                            ))

                # 取消时跳过 flush 编码（半成品随后整体删除，见下方清理）
                if not cancelled:
                    for p in output_stream.encode():
                        output_container.mux(p)
                    if out_audio_stream is not None:
                        for p in out_audio_stream.encode():
                            output_container.mux(p)
            finally:
                output_container.close()
        finally:
            input_container.close()

        if cancelled:
            # AUD-17231：取消路径与异常路径同样清理半成品（容器已在上方
            # finally 中关闭），避免截断文件随后被识别为 ready 代理
            logger.debug(f"PyAV video proxy cancelled, removing partial: {proxy_path}")
            _remove_quietly(proxy_path)
            return None

        if not os.path.exists(proxy_path) or os.path.getsize(proxy_path) == 0:
            logger.error(f"PyAV proxy not created: {proxy_path}")
            return None

        original_size = os.path.getsize(asset.path)
        proxy_size = os.path.getsize(proxy_path)
        ratio = proxy_size / original_size if original_size > 0 else 1.0
        logger.info(
            f"Video proxy generated: {proxy_path} "
            f"({proxy_size / 1024 / 1024:.1f}MB, {ratio:.1%} of original)"
        )
        if progress_callback:
            progress_callback(100)
        return proxy_path

    except Exception as e:
        logger.debug(f"PyAV video proxy failed: {e}")
        _remove_quietly(proxy_path)
        return None


def _generate_video_proxy_cli(
    asset: MediaAsset,
    proxy_path: str,
    config: ProxyConfig,
    progress_callback: Optional[Any] = None
) -> Optional[str]:
    """ffmpeg CLI 兜底路径（二进制经 ffmpeg_locator 定位）。"""
    # Build FFmpeg command
    # - Scale to 720p width (maintain aspect ratio)
    # - Use libx264 with CRF 23 (medium quality)
    # - Use medium preset for reasonable encoding speed
    # - Copy audio with AAC encoding at 128kbps
    ffmpeg_bin = find_ffmpeg() or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-i", asset.path,
        "-vf", f"scale={config.video_width}:-2",  # -2 ensures divisible by 2
        "-c:v", config.video_codec,
        "-crf", str(config.video_crf),
        "-preset", config.video_preset,
        "-c:a", config.audio_codec,
        "-b:a", config.audio_bitrate,
        "-y",  # Overwrite output file
        proxy_path
    ]

    logger.debug(f"FFmpeg command: {' '.join(cmd)}")

    process = None
    try:
        # Run FFmpeg
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wait for completion
        stdout, stderr = process.communicate(timeout=300)  # 5 min timeout

        if process.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace")
            logger.error(f"FFmpeg error: {error_msg}")
            _remove_quietly(proxy_path)  # AUD-17232：失败分支清理坏输出
            return None

        # Verify proxy file was created
        if os.path.exists(proxy_path):
            original_size = os.path.getsize(asset.path)
            proxy_size = os.path.getsize(proxy_path)
            ratio = proxy_size / original_size if original_size > 0 else 1.0

            logger.info(
                f"Video proxy generated: {proxy_path} "
                f"({proxy_size / 1024 / 1024:.1f}MB, {ratio:.1%} of original)"
            )

            if progress_callback:
                progress_callback(100)

            return proxy_path
        else:
            logger.error(f"Proxy file not created: {proxy_path}")
            return None

    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg timeout for {asset.path}")
        if process is not None:
            process.kill()
            # AUD-17232：kill 后回收子进程（排空管道），避免僵尸态
            try:
                process.communicate(timeout=5)
            except (OSError, ValueError, subprocess.TimeoutExpired) as reap_exc:
                logger.warning(f"Failed to reap FFmpeg process: {reap_exc}")
        _remove_quietly(proxy_path)  # AUD-17232：超时残留的部分输出一并清理
        return None
    except FileNotFoundError:
        logger.error("FFmpeg not found and PyAV unavailable; cannot generate proxy.")
        return None
    except Exception as e:
        logger.error(f"FFmpeg execution error: {e}")
        _remove_quietly(proxy_path)  # AUD-17232：失败分支清理坏输出
        return None


# ================================================================
# Image Proxy Generation
# ================================================================

def _generate_image_proxy(
    asset: MediaAsset,
    proxy_path: str,
    config: ProxyConfig,
    progress_callback: Optional[Any] = None
) -> Optional[str]:
    """
    Generate image proxy by resizing（PyAV 主路径，CLI 兜底）.

    Creates a JPEG proxy fitted within ``image_max_size`` (no upscaling).

    Args:
        asset: Source image asset
        proxy_path: Output proxy file path
        config: Proxy configuration
        progress_callback: Progress callback

    Returns:
        Proxy file path, or None if failed
    """
    # Change extension to match output format
    base_path = os.path.splitext(proxy_path)[0]
    proxy_path = f"{base_path}.{config.image_format}"

    logger.info(f"Generating image proxy: {asset.path} -> {proxy_path}")

    proxy = _generate_image_proxy_pyav(asset, proxy_path, config)
    if proxy is not None:
        if progress_callback:
            progress_callback(100)
        return proxy

    logger.debug("PyAV image proxy unavailable; falling back to ffmpeg CLI")
    return _generate_image_proxy_cli(asset, proxy_path, config, progress_callback)


def _generate_image_proxy_pyav(
    asset: MediaAsset,
    proxy_path: str,
    config: ProxyConfig,
) -> Optional[str]:
    """PyAV 解码并缩放图片代理；失败返回 None 交由 CLI 兜底。"""
    try:
        import av

        input_container = av.open(asset.path)
        try:
            if not input_container.streams.video:
                return None
            stream = input_container.streams.video[0]
            for frame in input_container.decode(stream):
                src_w = int(frame.width)
                src_h = int(frame.height)
                scale = min(
                    config.image_max_size / src_w,
                    config.image_max_size / src_h,
                    1.0,
                )
                out_w = max(2, int(src_w * scale) // 2 * 2)
                out_h = max(2, int(src_h * scale) // 2 * 2)
                scaled = frame.reformat(
                    width=out_w, height=out_h, format="yuvj420p",
                )

                output_container = av.open(proxy_path, "w")
                try:
                    out_stream = output_container.add_stream("mjpeg", rate=1)
                    out_stream.width, out_stream.height = out_w, out_h
                    out_stream.pix_fmt = "yuvj420p"
                    # 与 CLI `-q:v (100-quality)` 对齐（2=最佳，31=最差）
                    out_stream.codec_context.options = {
                        "qscale": str(max(2, int(100 - config.image_quality))),
                    }
                    for packet in out_stream.encode(scaled):
                        output_container.mux(packet)
                    for packet in out_stream.encode():
                        output_container.mux(packet)
                finally:
                    output_container.close()
                break
        finally:
            input_container.close()

        if not os.path.exists(proxy_path) or os.path.getsize(proxy_path) == 0:
            return None

        original_size = os.path.getsize(asset.path)
        proxy_size = os.path.getsize(proxy_path)
        ratio = proxy_size / original_size if original_size > 0 else 1.0
        logger.info(
            f"Image proxy generated: {proxy_path} "
            f"({proxy_size / 1024:.1f}KB, {ratio:.1%} of original)"
        )
        return proxy_path

    except Exception as e:
        logger.debug(f"PyAV image proxy failed: {e}")
        _remove_quietly(proxy_path)
        return None


def _generate_image_proxy_cli(
    asset: MediaAsset,
    proxy_path: str,
    config: ProxyConfig,
    progress_callback: Optional[Any] = None
) -> Optional[str]:
    """ffmpeg CLI 兜底路径（二进制经 ffmpeg_locator 定位）。"""
    ffmpeg_bin = find_ffmpeg() or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-i", asset.path,
        "-vf", f"scale={config.image_max_size}:-2",  # Scale to max dimension
        "-q:v", str(int(100 - config.image_quality)),  # JPEG quality (inverted)
        "-y",  # Overwrite output file
        proxy_path
    ]

    try:
        process = subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,  # 1 min timeout for images
        )

        if process.returncode != 0:
            error_msg = process.stderr.decode("utf-8", errors="replace")
            logger.error(f"FFmpeg error: {error_msg}")
            return None

        # Verify proxy file was created
        if os.path.exists(proxy_path):
            original_size = os.path.getsize(asset.path)
            proxy_size = os.path.getsize(proxy_path)
            ratio = proxy_size / original_size if original_size > 0 else 1.0

            logger.info(
                f"Image proxy generated: {proxy_path} "
                f"({proxy_size / 1024:.1f}KB, {ratio:.1%} of original)"
            )

            if progress_callback:
                progress_callback(100)

            return proxy_path
        else:
            logger.error(f"Proxy file not created: {proxy_path}")
            return None

    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg timeout for {asset.path}")
        return None
    except FileNotFoundError:
        logger.error("FFmpeg not found and PyAV unavailable; cannot generate proxy.")
        return None
    except Exception as e:
        logger.error(f"Image proxy generation error: {e}")
        return None


# ================================================================
# Utility Functions
# ================================================================

def _container_duration_seconds(input_container: Any, input_stream: Any) -> float:
    """估算容器/视频流总时长（秒），未知返回 0。

    AUD-17233：进度归一化分母。流级 duration*time_base 更精确；容器级
    duration 兜底（部分封装只在容器层报告时长）。任何异常按未知处理。
    """
    try:
        stream_duration = getattr(input_stream, "duration", None)
        time_base = getattr(input_stream, "time_base", None)
        if stream_duration is not None and time_base is not None:
            seconds = float(stream_duration) * float(time_base)
            if seconds > 0.0:
                return seconds
        container_duration = getattr(input_container, "duration", None)
        if container_duration is not None:
            seconds = float(container_duration)
            if seconds > 0.0:
                return seconds
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    return 0.0


def _encode_progress_percent(frame_time: Optional[float], total_seconds: float) -> int:
    """当前帧时间 → 0..95 编码进度（95 留给 mux 收尾，100 在成功后发）。

    AUD-17233：已知总时长时按 frame_time/total_seconds 归一化；时长未知
    时退回旧的墙钟估计（time*100 截 95）并保持单调不减。
    """
    try:
        current = float(frame_time or 0.0)
    except (TypeError, ValueError):
        current = 0.0
    if total_seconds > 0.0:
        ratio = max(0.0, min(1.0, current / total_seconds))
        return int(ratio * 95.0)
    return min(95, int(current * 100.0))


def _resample_audio(resampler: Any, frame: Any) -> list:
    """兼容不同 PyAV 版本的 resample 返回值（list / 单帧 / None）。"""
    result = resampler.resample(frame)
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return [result]


def _remove_quietly(path: str) -> None:
    """删除失败遗留的部分输出文件（静默容错）。"""
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass

def _generate_proxy_path(original_path: str, suffix: str) -> str:
    """
    Generate proxy file path from original path.

    Args:
        original_path: Original file path
        suffix: Proxy suffix (e.g., "_proxy")

    Returns:
        Proxy file path (e.g., "/path/to/file_proxy.mp4")
    """
    directory = os.path.dirname(original_path)
    filename = os.path.basename(original_path)
    name, ext = os.path.splitext(filename)

    proxy_filename = f"{name}{suffix}{ext}"
    return os.path.join(directory, proxy_filename)


def check_ffmpeg_available() -> bool:
    """
    Check whether media transcoding is available.

    PyAV（自带 FFmpeg 库，打包后开箱即用）或 ffmpeg 可执行文件
    （ffmpeg_locator：打包 bin/ → PATH）任一可用即返回 True。
    """
    try:
        from src.shared.media.probe import check_ffmpeg_available as _pyav_ok
        if _pyav_ok():
            return True
    except ImportError:
        pass

    ffmpeg_bin = find_ffmpeg()
    if ffmpeg_bin is None:
        return False
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def get_proxy_path(asset: MediaAsset, config: Optional[ProxyConfig] = None) -> Optional[str]:
    """
    Get the expected proxy path for an asset.

    Does not generate the proxy, just returns the path where it would be created.

    Args:
        asset: MediaAsset
        config: Proxy configuration

    Returns:
        Expected proxy file path, or None if asset path is invalid
    """
    config = config or DEFAULT_PROXY_CONFIG

    if not asset.path:
        return None

    return _generate_proxy_path(asset.path, config.suffix)


def delete_proxy(asset: MediaAsset) -> bool:
    """
    Delete proxy file for an asset.

    Args:
        asset: MediaAsset

    Returns:
        True if proxy was deleted, False otherwise
    """
    if not asset.proxy_path:
        return False

    try:
        if os.path.exists(asset.proxy_path):
            os.remove(asset.proxy_path)
            logger.info(f"Deleted proxy: {asset.proxy_path}")
            return True
        return False
    except OSError as e:
        logger.error(f"Failed to delete proxy {asset.proxy_path}: {e}")
        return False


def delete_all_proxies_in_directory(directory: str, suffix: str = "_proxy") -> int:
    """
    Delete all proxy files in a directory.

    AUD-17235 修复说明：只删除能回溯到同目录真实源文件的
    代理条目。代理命名与 ``_generate_proxy_path`` 一致（源名 + suffix +
    同扩展名）：目录内存在 ``name.ext`` 时才允许删除 ``name+suffix+ext``。
    子串匹配（旧实现）会把文件名恰好含 suffix 的用户原片（如
    "interview_proxy_reel.mov"）当代理永久删除。

    Args:
        directory: Directory to search
        suffix: Proxy suffix to match（空 suffix 无合法语义，直接返回 0）

    Returns:
        Number of proxy files deleted
    """
    deleted_count = 0
    if not suffix:
        return 0

    try:
        entries = os.listdir(directory)
    except OSError as e:
        logger.error(f"Failed to list directory {directory}: {e}")
        return deleted_count

    files = {
        name for name in entries
        if os.path.isfile(os.path.join(directory, name))
    }
    # 由真实源文件推导合法代理文件名集合（与 _generate_proxy_path 完全
    # 一致的 name+suffix+ext 模式），只删除落在这个集合内的文件。
    expected_proxy_names = {
        f"{stem}{suffix}{ext}"
        for stem, ext in (os.path.splitext(name) for name in files)
        if ext  # 无扩展名的源文件不可能按本模块规则生成代理
    }

    for filename in sorted(files & expected_proxy_names):
        filepath = os.path.join(directory, filename)
        try:
            os.remove(filepath)
            deleted_count += 1
            logger.debug(f"Deleted proxy: {filepath}")
        except OSError as e:
            logger.warning(f"Failed to delete {filepath}: {e}")

    return deleted_count
