"""
Proxy Generator Module
代理文件生成器，使用 FFmpeg 生成磁盘代理文件。

## 职责（P10.3）

- ProxyGenerator: 代理文件生成器
- ProxyGenerationResult: 生成结果数据类
- ProxyProgress: 生成进度数据类

## 设计原则

- 在后台线程执行，不阻塞 UI
- 支持取消生成
- 完整的错误处理
- 支持进度回调
- 完整类型注解

## 使用示例

```python
from src.shared.media.proxy_generator import ProxyGenerator, ProxyProgress
from src.shared.media.proxy import DiskProxyConfig, ProxyFormat, ProxyResolution

# 创建配置
config = DiskProxyConfig(
    format=ProxyFormat.PRORES_PROXY,
    resolution=ProxyResolution.HALF,
)

# 创建生成器
generator = ProxyGenerator(config)

# 定义进度回调
def on_progress(progress: ProxyProgress) -> None:
    print(f"Progress: {progress.percent:.1f}%")

# 生成代理（在后台线程中调用）
result = generator.generate(
    source_path='/path/to/video.mp4',
    progress_callback=on_progress,
)

if result.success:
    print(f"Proxy created: {result.proxy_path}")
else:
    print(f"Failed: {result.error_message}")
```

## 架构位置

```text
ProxyManager
    └── ProxyGenerator (FFmpeg subprocess)
            ├── probe source (ffprobe)
            ├── calculate target size
            ├── run FFmpeg encode
            └── verify output
```
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional, Callable, TYPE_CHECKING

from src.shared.ffmpeg_locator import find_ffmpeg, find_ffprobe

from .proxy import (
    DiskProxyConfig,
    ProxyFormat,
    generate_proxy_key,
    get_source_file_info,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# =============================================================================
# 数据类
# =============================================================================

@dataclass
class ProxyProgress:
    """
    代理生成进度。

    Attributes:
        percent: 完成百分比（0.0-100.0）
        current_frame: 当前帧号
        total_frames: 总帧数
        fps: 当前编码帧率
        elapsed_seconds: 已用时间（秒）
        estimated_remaining_seconds: 预计剩余时间（秒）
        status_message: 状态消息
    """
    percent: float = 0.0
    current_frame: int = 0
    total_frames: int = 0
    fps: float = 0.0
    elapsed_seconds: float = 0.0
    estimated_remaining_seconds: float = 0.0
    status_message: str = "Starting..."


@dataclass
class ProxyGenerationResult:
    """
    代理生成结果。

    Attributes:
        success: 是否成功
        source_path: 源文件路径
        proxy_path: 代理文件路径（成功时有效）
        proxy_size_bytes: 代理文件大小（字节）
        proxy_resolution: 代理分辨率 (width, height)
        duration_seconds: 视频时长
        elapsed_seconds: 生成耗时
        error_message: 错误消息（失败时有效）
        cancelled: 是否被取消
    """
    success: bool = False
    source_path: str = ""
    proxy_path: str = ""
    proxy_size_bytes: int = 0
    proxy_resolution: tuple[int, int] = (0, 0)
    duration_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    error_message: str = ""
    cancelled: bool = False


# =============================================================================
# ProxyGenerator
# =============================================================================

class ProxyGenerator:
    """
    代理文件生成器。

    使用 FFmpeg 生成磁盘代理文件。支持 ProRes Proxy、H.264、图像序列格式。
    生成过程在后台执行，支持进度回调和取消。

    Attributes:
        config: 代理配置
        _cancelled: 取消标志
        _process: 当前 FFmpeg 进程

    Examples:
        >>> generator = ProxyGenerator(DiskProxyConfig())
        >>> result = generator.generate('/path/to/video.mp4')
        >>> result.success
        True
    """

    def __init__(self, config: DiskProxyConfig) -> None:
        """
        初始化生成器。

        Args:
            config: 磁盘代理配置
        """
        self.config = config
        self._cancelled = False
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._ffmpeg_bin: Optional[str] = None
        self._lock = threading.Lock()
        # AUD-35609：进程内编码路径（PyAV/_run_topos）的协作式暂停。
        # 置位时帧循环检查点让出 CPU 等待恢复；CLI 路径仍走 SIGSTOP。
        self._pause_event = threading.Event()
        self._inprocess_encode: bool = False

    def generate(
        self,
        source_path: str,
        progress_callback: Optional[Callable[[ProxyProgress], None]] = None,
    ) -> ProxyGenerationResult:
        """
        生成代理文件。

        这是主要入口方法。在后台线程中执行 FFmpeg 编码。

        Args:
            source_path: 源文件路径
            progress_callback: 进度回调函数

        Returns:
            ProxyGenerationResult 生成结果

        Raises:
            FileNotFoundError: 源文件不存在
            RuntimeError: FFmpeg 不可用
        """
        import time

        start_time = time.time()
        # AUD-35605：此处不再无条件重置 _cancelled。生成器实例为单次使用
        # （ProxyManager 每个任务新建一个 ProxyGenerator，构造函数已初始化
        # _cancelled = False）；若在任务置 generating 与本方法启动之间的
        # 窗口内 cancel() 已置位，重置会吞掉该取消，任务完整跑完编码并把
        # 状态覆盖为 complete。保留标志可让编码循环立即以 cancelled 退出。
        # 清掉上一次运行可能残留的暂停标志（AUD-35609），避免新任务
        # 在帧循环检查点无端停摆。
        self._pause_event.clear()

        # 验证源文件
        if not os.path.isfile(source_path):
            return ProxyGenerationResult(
                success=False,
                source_path=source_path,
                error_message=f"Source file not found: {source_path}",
            )

        # P8-Topos：TPIC 源（自研容器，PyAV/ffmpeg 均无法解码）→ 走自研
        # decode→encode 链（ToposMediaSource 解码 + box 缩小 + ToposCodec
        # 编码，输出同为 TPIC——播放侧 factory 内容嗅探自动路由，零改动）
        is_topos_source = False
        try:
            from .topos_source import is_topos_movie
            is_topos_source = bool(is_topos_movie(source_path))
        except Exception as e:
            logger.debug(f"Topos content sniff failed for {source_path}: {e}")

        # topos 代理档（普通源）：PyAV 解码 + native topos 编码。topos 源
        # 已在上方强制走 _run_topos，与此分支互斥。
        use_topos_encode = (
            not is_topos_source
            and self.config.format == ProxyFormat.TOPOS_PROXY
        )
        # PyAV 优先（进程内解码，打包后开箱即用，无需系统 ffmpeg）；
        # 图像序列格式仅 CLI 支持时走 ffmpeg（二进制经 ffmpeg_locator 定位）。
        use_pyav = (
            not is_topos_source
            and not use_topos_encode
            and self.config.format != ProxyFormat.IMAGE_SEQUENCE
            and self._pyav_usable()
        )

        try:
            # 探测源文件
            source_info = (
                self._probe_topos(source_path)
                if is_topos_source
                else self._probe_source(source_path)
            )
            if source_info is None:
                return ProxyGenerationResult(
                    success=False,
                    source_path=source_path,
                    error_message="Failed to probe source file",
                )

            width, height, fps, duration, total_frames = source_info

            # 计算目标分辨率
            target_size = self.config.calculate_target_size((width, height))

            # 生成代理键
            source_mtime, source_size = get_source_file_info(source_path)
            proxy_key = generate_proxy_key(
                source_path=source_path,
                source_mtime=source_mtime,
                source_size=source_size,
                proxy_config=self.config,
                fps=fps,
            )

            # 生成输出路径
            proxy_path = self.config.get_output_filename(source_path, proxy_key)

            # 确保输出目录存在
            output_dir = os.path.dirname(proxy_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)

            # 更新进度
            if progress_callback:
                progress_callback(ProxyProgress(
                    percent=5.0,
                    status_message="Preparing encoding...",
                ))

            # 更新进度
            if progress_callback:
                progress_callback(ProxyProgress(
                    percent=10.0,
                    total_frames=total_frames,
                    status_message="Starting proxy encode...",
                ))

            if is_topos_source:
                with self._lock:
                    self._inprocess_encode = True
                result = self._run_topos(
                    source_path=source_path,
                    proxy_path=proxy_path,
                    target_size=target_size,
                    total_frames=total_frames,
                    start_time=start_time,
                    progress_callback=progress_callback,
                )
            elif use_topos_encode:
                with self._lock:
                    self._inprocess_encode = True
                result = self._run_pyav_to_topos(
                    source_path=source_path,
                    proxy_path=proxy_path,
                    target_size=target_size,
                    total_frames=total_frames,
                    start_time=start_time,
                    progress_callback=progress_callback,
                )
            elif use_pyav:
                with self._lock:
                    self._inprocess_encode = True
                result = self._run_pyav(
                    source_path=source_path,
                    proxy_path=proxy_path,
                    target_size=target_size,
                    total_frames=total_frames,
                    start_time=start_time,
                    progress_callback=progress_callback,
                )
            elif self._check_ffmpeg():
                ffmpeg_cmd = self._build_ffmpeg_command(
                    source_path=source_path,
                    output_path=proxy_path,
                    target_size=target_size,
                )
                result = self._run_ffmpeg(
                    ffmpeg_cmd=ffmpeg_cmd,
                    source_path=source_path,
                    proxy_path=proxy_path,
                    total_frames=total_frames,
                    start_time=start_time,
                    progress_callback=progress_callback,
                    source_fps=fps,
                )
            else:
                result = ProxyGenerationResult(
                    success=False,
                    source_path=source_path,
                    error_message=(
                        "Neither PyAV nor an ffmpeg executable is available "
                        "for proxy generation"
                    ),
                    elapsed_seconds=time.time() - start_time,
                )

            return result

        except Exception as e:
            logger.exception(f"Proxy generation failed for {source_path}")
            return ProxyGenerationResult(
                success=False,
                source_path=source_path,
                error_message=str(e),
                elapsed_seconds=time.time() - start_time,
            )
        finally:
            # AUD-35609：无论成功/失败/异常，进程内编码标志必须复位，
            # 否则后续 pause() 会误认为仍在编码而虚报成功。
            with self._lock:
                self._inprocess_encode = False

    def cancel(self) -> None:
        """取消当前生成任务。"""
        import signal

        with self._lock:
            self._cancelled = True
            if self._process is not None:
                # 被 pause() 挂起的进程无法读取 stdin 的退出指令，
                # 先恢复运行再请求退出，避免监控线程永久阻塞。
                if hasattr(signal, "SIGCONT"):
                    try:
                        self._process.send_signal(signal.SIGCONT)
                    except (ProcessLookupError, OSError):
                        pass
                try:
                    # 发送 'q' 到 FFmpeg 优雅退出
                    self._process.stdin.write(b'q')
                    self._process.stdin.flush()
                except Exception:
                    # 如果优雅退出失败，强制终止
                    self._process.kill()
                self._process = None

    def pause(self) -> bool:
        """
        挂起当前编码任务，不丢弃已完成的进度。

        用于交互式前台任务（如场景剪切探测）运行期间让出 CPU；
        配合 resume() 恢复。两类路径（AUD-35609）：

        - CLI 路径（_run_ffmpeg）：SIGSTOP 挂起 ffmpeg 进程；
        - 进程内路径（_run_pyav / _run_pyav_to_topos / _run_topos，
          此前恒返回 False、pause_all_generation 对主路径无效）：
          置位 _pause_event，帧循环检查点协作式等待恢复。

        Returns:
            是否成功挂起
        """
        import signal

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                if not hasattr(signal, "SIGSTOP"):
                    return False
                try:
                    self._process.send_signal(signal.SIGSTOP)
                    return True
                except (ProcessLookupError, OSError):
                    return False
            if self._inprocess_encode:
                self._pause_event.set()
                return True
            return False

    def resume(self) -> bool:
        """
        恢复被 pause() 挂起的编码任务（SIGCONT 或清除协作式暂停标志）。

        Returns:
            是否成功恢复
        """
        import signal

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                if not hasattr(signal, "SIGCONT"):
                    return False
                try:
                    self._process.send_signal(signal.SIGCONT)
                    return True
                except (ProcessLookupError, OSError):
                    return False
            if self._pause_event.is_set():
                self._pause_event.clear()
                return True
            return False

    def _wait_if_paused(self) -> None:
        """协作式暂停检查点：被 pause() 置位期间让出 CPU。

        以 0.2s 粒度轮询，cancel() 置位 _cancelled 后立即退出等待，
        保证暂停期间取消仍然即时生效。调用方不得持有 self._lock。
        """
        while self._pause_event.is_set() and not self._cancelled:
            self._pause_event.wait(0.2)

    @property
    def is_cancelled(self) -> bool:
        """是否已取消"""
        return self._cancelled

    def _check_ffmpeg(self) -> bool:
        """检查 ffmpeg CLI 是否可用（经 ffmpeg_locator：打包 bin/ → PATH）"""
        resolved = find_ffmpeg()
        if resolved is None:
            return False
        try:
            result = subprocess.run(
                [resolved, '-version'],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                self._ffmpeg_bin = resolved
                return True
            return False
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def _pyav_usable(self) -> bool:
        """PyAV 及当前代理格式所需编码器是否可用（打包环境的主路径）。"""
        try:
            import av
        except ImportError:
            return False
        codec = (
            'prores_ks'
            if self.config.format == ProxyFormat.PRORES_PROXY
            else 'libx264'
        )
        return codec in getattr(av, 'codecs_available', set())

    def _probe_topos(
        self,
        source_path: str
    ) -> Optional[tuple[int, int, float, float, int]]:
        """探测 Topos 源（TPIC MOV）：经自研解码后端，PyAV/ffprobe 均不可用。

        Returns:
            (width, height, fps, duration, total_frames) 或 None
        """
        try:
            from .topos_source import ToposMediaSource
        except ImportError:
            logger.warning("Topos proxy: topos_source 不可用")
            return None
        try:
            src = ToposMediaSource(source_path)
            src.open()
            try:
                md = src.probe()
            finally:
                src.close()
        except Exception as e:
            logger.warning(f"Topos proxy: probe failed for {source_path}: {e}")
            return None
        fps = float(md.fps)
        if fps <= 0:
            fps = 24.0
        return (
            md.width, md.height, fps,
            float(md.duration_seconds or 0.0),
            int(md.total_frames or 0),
        )

    def _run_topos(
        self,
        source_path: str,
        proxy_path: str,
        target_size: tuple[int, int],
        total_frames: int,
        start_time: float,
        progress_callback: Optional[Callable[[ProxyProgress], None]] = None,
    ) -> ProxyGenerationResult:
        """TPIC 源的代理生成：自研 decode→（box 缩小）→自研 encode。

        输出为 TPIC 代理（比配置格式更优：播放走同一 planar 后端，且
        ToposMediaSource.target_size 的分离 box 滤波即缩小路径）。取消/
        失败不留半成品文件（未 finish 的 MOV 无 moov，但仍然删除）。
        """
        import time

        import numpy as np

        def _fail(message: str, *, cancelled: bool = False) -> ProxyGenerationResult:
            try:
                if os.path.exists(proxy_path):
                    os.remove(proxy_path)
            except OSError:
                pass
            return ProxyGenerationResult(
                success=False,
                source_path=source_path,
                cancelled=cancelled,
                error_message=message,
                elapsed_seconds=time.time() - start_time,
            )

        try:
            from .topos_source import ToposMediaSource
            from src.shared.codec.topos_binding import (
                TOPOS_CODEC_ABI_VERSION,
                TOPOS_MATRIX_CODES,
                TOPOS_PRIMARIES_CODES,
                TOPOS_TRANSFER_CODES,
                ToposCodec,
                ToposCodecError,
                ToposMuxFile,
                _CFrameConfig,
            )
            from src.shared.codec.topos_profiles import get_topos_tier
            from src.shared.export.topos_rate_control import ToposRateFeedback
        except ImportError as e:
            return _fail(f"Topos proxy chain unavailable: {e}")

        tier = get_topos_tier('proxy')
        target_bytes = (
            tier.frame_target_bytes(target_size[0], target_size[1])
            if tier.is_rate_controlled else None
        )

        src: Optional[ToposMediaSource] = None
        mux = None
        try:
            src = ToposMediaSource(source_path, target_size=target_size)
            src.open()
            md = src.probe()

            # 实际编码尺寸必须取自解码平面（target_size 缩小后 probe 仍报告
            # 源原始分辨率——用原始尺寸配 movie_config 会与平面静默错配）
            first = src.read_frame(0)
            ph, pw = first.planes[0].shape[:2]

            num = int(getattr(md.fps, 'num', 0) or 0)
            den = int(getattr(md.fps, 'den', 1) or 1)
            if num <= 0 or den <= 0:
                num, den = 24, 1

            # 色彩码：沿用源 tpcC（未知标签显式失败，不默认掩盖——R-13）
            primaries = TOPOS_PRIMARIES_CODES.get(str(md.color_primaries or '').lower())
            transfer = TOPOS_TRANSFER_CODES.get(str(md.color_transfer or '').lower())
            matrix = TOPOS_MATRIX_CODES.get(str(md.color_matrix or '').lower())
            if primaries is None or transfer is None or matrix is None:
                return _fail(
                    f"Topos proxy: 源色彩标签无法映射（primaries={md.color_primaries!r}, "
                    f"transfer={md.color_transfer!r}, matrix={md.color_matrix!r}）"
                )
            rng_s = str(md.color_range or '').lower()
            if rng_s not in ('', 'limited', 'full'):
                return _fail(
                    f"Topos proxy: 源色彩 range 无法映射（{md.color_range!r}）"
                )
            range_code = 1 if rng_s == 'full' else 0

            has_alpha = bool(md.has_alpha)
            source_fmt = str(getattr(first, "pixel_format", "") or
                             getattr(md, "pixel_format", "") or "").lower()
            if "444" in source_fmt and "yuv" in source_fmt:
                source_layout = "yuv444"
            elif "422" in source_fmt and "yuv" in source_fmt:
                source_layout = "yuv422"
            elif "gbr" in source_fmt:
                # G4：GBR（pf=2，(G,B,R) 恒等序 full-range）源——逐帧显式
                # RGB→YUV（bt709/limited）转换后进代理（此前显式拒收且无
                # 回退，GBR 影片只能全分辨率播放）。转换数学与主导出线
                # _rgb_to_yuv_planar 同源。
                source_layout = "gbr"
            else:
                return _fail(
                    "Topos proxy: 仅支持 YUV 4:2:2/4:4:4/GBR 源，"
                    f"不支持 {source_fmt!r}"
                )
            if source_layout == "gbr":
                # 转换产物 = limited-range bt709 YUV——代理标签必须如实
                # （GBR 源的 identity matrix/full range 标签对 YUV 载荷
                # 非法：native 交叉规则 'YUV payload cannot use GBR
                # identity matrix'）。G4 边缘（2026-09-21 复查告警）：
                # HDR/宽色域 GBR 源经 bt709+clip 近似（高光裁切/色域收拢
                # 而代理沿用源 primaries/transfer 标签）——按 W1 姿态
                # 显式告警一次，不静默。
                src_primaries = str(
                    getattr(md, "color_primaries", "") or "").lower()
                src_transfer = str(
                    getattr(md, "color_transfer", "") or "").lower()
                if (src_primaries not in ("", "bt709", "smpte170m")
                        or src_transfer in ("smpte2084", "pq",
                                            "arib-std-b67", "hlg")):
                    logger.warning(
                        "Topos proxy: GBR 源 primaries=%r transfer=%r 经 "
                        "bt709/limited 近似转换生成代理（HDR 高光/宽色域 "
                        "将被裁切收拢；代理预览口径，不影响母版）",
                        src_primaries, src_transfer)
                matrix = TOPOS_MATRIX_CODES['bt709']
                range_code = 0
            source_color_depth = int(getattr(md, "bit_depth", 10) or 10)
            # G12：代理位深/色度结构/QP 参数化（DiskProxyConfig；默认
            # 10-bit/422/qp34 = 既有口径不变）。位深取 min(请求, 源)——
            # 上采样无收益；12-bit 源 + 12-bit 请求不再强制右移量化。
            target_color_depth = min(
                int(getattr(self.config, 'topos_bit_depth', 10) or 10),
                source_color_depth)
            keep_444 = (bool(getattr(self.config, 'topos_keep_444', False))
                        and source_layout in ("yuv444", "gbr"))
            proxy_qp = int(getattr(self.config, 'topos_qp', 34) or 34)
            # R1（审计 P1-1 同类）：alpha 位深取自解码平面的权威声明，
            # 不得硬编码 12——a8 源会被压成近全透明、a16 源会被静默截断。
            alpha_bit_depth = 12
            alpha_premult = False
            if has_alpha:
                p_infos = getattr(first, 'plane_infos', None) or ()
                if len(p_infos) >= 4:
                    alpha_bit_depth = int(p_infos[3].bit_depth)
                alpha_premult = bool(
                    (getattr(first, 'extra', None) or {}).get(
                        'topos_alpha_premultiplied', False))
            # a16 仅 mode1 无损可承载；8/10/12 走受限近似 mode2
            alpha_mode = (1 if alpha_bit_depth == 16 else 2) if has_alpha else 0

            codec = ToposCodec()
            movie_cfg = codec.movie_config(
                pw, ph,
                qp=proxy_qp, qmatrix=1,
                bit_depth=target_color_depth,
                pixel_format=1 if keep_444 else 0,
                alpha_mode=alpha_mode,
                alpha_bit_depth=alpha_bit_depth if has_alpha else 0,
                alpha_premultiplied=alpha_premult,
                color_range=range_code,
                color_primaries=primaries,
                color_transfer=transfer,
                color_matrix=matrix,
                timescale=num,
            )

            import ctypes
            fc = _CFrameConfig()
            fc.struct_size = ctypes.sizeof(_CFrameConfig)
            fc.abi_version = TOPOS_CODEC_ABI_VERSION
            fc.visible_width, fc.visible_height = pw, ph
            fc.profile = 3
            fc.pixel_format = 1 if keep_444 else 0
            fc.bit_depth = target_color_depth
            fc.qp_base = proxy_qp
            fc.qmatrix_id = 1
            fc.alpha_mode = alpha_mode
            fc.alpha_bit_depth = alpha_bit_depth if has_alpha else 0
            fc.alpha_premultiplied = int(alpha_premult)
            fc.color_range = range_code
            fc.color_primaries = primaries
            fc.color_transfer = transfer
            fc.color_matrix = matrix
            fc.sar_num, fc.sar_den = 1, 1
            # V 代际收纳（D3，2026-09-13）：显式钉 V7-R2（cfg em=8）——与
            # 主导出线（ToposVideoEncoder 缺省 'rans2'）口径一致。此前不设
            # reserved[0]（ctypes 零初始化→隐式 V1），依赖零值语义属脆弱
            # 隐式写点；显式赋值后代理口径不再随缺省漂移。
            fc.reserved[0] = 8

            mux = ToposMuxFile(codec, proxy_path, movie_cfg)
            # Keep the conversion on C-contiguous uint16 arrays.  The binding
            # accepts these by pointer, so the old per-frame ``tobytes()``
            # materialization is unnecessary.  Reuse target-sized scratch
            # buffers for 4:4:4 chroma reduction and Alpha rescaling.
            chroma_scratch = None
            if (source_layout in ("yuv444", "gbr") and not keep_444):
                chroma_scratch = [
                    np.empty((ph, (pw + 1) // 2), dtype="<u2")
                    for _ in range(2)
                ]
            color_depth_scratch = None
            if source_color_depth != target_color_depth:
                color_depth_scratch = [
                    np.empty_like(first.planes[index], dtype="<u2")
                    for index in range(3)
                ]
            alpha_scratch = None
            if has_alpha and alpha_bit_depth != 16:
                alpha_scratch = np.empty_like(first.planes[3], dtype="<u2")

            def _color_to_10(plane, index):
                if source_color_depth == target_color_depth:
                    return np.ascontiguousarray(plane)
                out = color_depth_scratch[index]
                if source_color_depth < target_color_depth:
                    np.left_shift(
                        plane, target_color_depth - source_color_depth, out=out)
                else:
                    shift = source_color_depth - target_color_depth
                    np.right_shift(plane, shift, out=out)
                    remainder = np.bitwise_and(plane, (1 << shift) - 1)
                    np.add(
                        out,
                        remainder >= (1 << (shift - 1)),
                        out=out,
                        casting="unsafe",
                    )
                    np.minimum(out, (1 << target_color_depth) - 1, out=out)
                return out

            def _proxy_planes(frame):
                if source_layout == "gbr":
                    from src.shared.export.topos_encoder import (
                        _rgb_to_yuv_planar,
                    )

                    full = float((1 << source_color_depth) - 1)
                    rgb = np.stack([
                        frame.planes[2], frame.planes[0], frame.planes[1],
                    ], axis=-1).astype(np.float32) / full
                    colors = list(_rgb_to_yuv_planar(
                        rgb, 'bt709', full_range=False,
                        bit_depth=target_color_depth, chroma_full=True))
                else:
                    colors = [
                        _color_to_10(frame.planes[index], index)
                        for index in range(3)
                    ]
                if source_layout in ("yuv444", "gbr") and not keep_444:
                    chroma = []
                    for index, plane in enumerate(colors[1:3]):
                        if plane.shape[1] & 1:
                            plane = np.pad(plane, ((0, 0), (0, 1)), mode="edge")
                        out = chroma_scratch[index]
                        np.add(plane[:, 0::2], plane[:, 1::2], out=out)
                        np.add(out, 1, out=out)
                        np.right_shift(out, 1, out=out)
                        chroma.append(out)
                    colors[1:] = chroma
                planes = colors
                if has_alpha and len(frame.planes) >= 4:
                    if alpha_bit_depth == 16:
                        planes.append(np.ascontiguousarray(frame.planes[3]))
                    else:
                        np.left_shift(
                            frame.planes[3], 16 - alpha_bit_depth,
                            out=alpha_scratch)
                        planes.append(alpha_scratch)
                return planes

            last_progress_at = 0.0
            # R3：alpha 预算统计闭环（record-only——代理可再生，超限仅警告
            # + tpcB 记录，不抛错不降档；位深沿用源声明 = 格式契约）
            proxy_tier = get_topos_tier('proxy')
            budget = {
                'target': float(proxy_tier.alpha_budget_ratio),
                'cap': float(proxy_tier.alpha_hard_cap),
                'total_alpha': 0, 'total_color': 0, 'max_err': 0, 'frames': 0,
                'overrun': False,
            }
            # Keep proxy rate control consistent with ToposVideoEncoder:
            # one deterministic sized search establishes the first QP, then
            # each subsequent frame is a single native encode with feedback.
            # This removes a repeated QP search from the background proxy job.
            rate_feedback = None
            for i in range(int(md.total_frames or 0)):
                if self._cancelled:
                    mux.close()
                    return _fail("Generation cancelled by user", cancelled=True)
                # AUD-35609：进程内编码的协作式暂停检查点
                self._wait_if_paused()
                frame = src.read_frame(i)
                planes = _proxy_planes(frame)
                if target_bytes is not None and rate_feedback is None:
                    pkt, st, _qp = codec.encode_sized(
                        fc, planes, target_bytes, qp_min=0, qp_max=63)
                    rate_feedback = ToposRateFeedback(
                        target_bytes, qp_min=0, qp_max=63)
                    rate_feedback.seed(_qp, len(pkt))
                elif target_bytes is not None:
                    fc.qp_base = int(rate_feedback.qp)
                    pkt, st = codec.encode_frame(fc, planes)
                    rate_feedback.note(len(pkt))
                else:
                    pkt, st = codec.encode_frame(fc, planes)
                if has_alpha:
                    budget['total_alpha'] += int(st.alpha_payload_bytes)
                    budget['total_color'] += int(st.color_payload_bytes)
                    budget['max_err'] = max(budget['max_err'],
                                            int(st.alpha_max_abs_error))
                    budget['frames'] += 1
                mux.add_packet(pkt, pts=i * den, dur=den)

                now = time.time()
                if progress_callback and now - last_progress_at >= 0.5:
                    last_progress_at = now
                    done = i + 1
                    pct = 10.0 + 85.0 * done / max(1, int(md.total_frames or 1))
                    progress_callback(ProxyProgress(
                        percent=pct,
                        current_frame=done,
                        total_frames=int(md.total_frames or 0),
                        status_message="Encoding Topos proxy...",
                    ))
            if has_alpha and budget['frames'] > 0:
                actual = budget['total_alpha'] / max(1, budget['total_color'])
                if actual > budget['cap']:
                    budget['overrun'] = True
                    logger.warning(
                        "Topos proxy: alpha 预算超硬上限 alpha/color=%.3f "
                        "cap=%.2f（代理可再生——记录交付，不降档不抛错）",
                        actual, budget['cap'])
                mux.set_alpha_budget({
                    'target_ratio_bp': int(round(budget['target'] * 10000.0)),
                    'actual_ratio_bp': min(
                        65535, int(round(actual * 10000.0))),
                    'max_abs_error': budget['max_err'],
                    'flags': {'overrun': budget['overrun'],
                              'authorized': False, 'adapted': False},
                    'frame_count': budget['frames'],
                })
            mux.finish()
            mux.close()
            mux = None

            proxy_size = os.path.getsize(proxy_path) if os.path.exists(proxy_path) else 0
            return ProxyGenerationResult(
                success=True,
                source_path=source_path,
                proxy_path=proxy_path,
                proxy_size_bytes=proxy_size,
                proxy_resolution=(pw, ph),
                duration_seconds=float(md.duration_seconds or 0.0),
                elapsed_seconds=time.time() - start_time,
            )
        except ToposCodecError as e:
            return _fail(f"Topos proxy encode failed: {e}")
        except Exception as e:
            logger.exception(f"Topos proxy generation failed for {source_path}")
            return _fail(f"Topos proxy generation failed: {e}")
        finally:
            if mux is not None:
                try:
                    mux.close()
                except Exception:
                    pass
            if src is not None:
                try:
                    src.close()
                except Exception:
                    pass

    def _run_pyav_to_topos(
        self,
        *,
        source_path: str,
        proxy_path: str,
        target_size: tuple[int, int],
        total_frames: int,
        start_time: float,
        progress_callback: Optional[Callable[[ProxyProgress], None]],
    ) -> ProxyGenerationResult:
        """普通源的 topos 代理：PyAV 解码 → swscale 缩小 → native 编码。

        与 ``_run_topos``（TPIC 源）同一编码面：proxy 档 sized 码控
        （qp_min=0/qp_max=63）、V7-R2 熵、TPIC MOV 纯视频容器（ADR-C008，
        无音轨——与 topos 源代理一致）。色彩标签取 PyAV 流元数据，缺失
        时默认 bt709（代理可再生缓存，与 TPIC 源的"显式失败"不同——
        那里元数据是权威声明，这里源千奇百怪，猜错成本只是重生成）。
        PyAV 不可解码 / native codec 缺失时显式失败，不静默回落 ProRes。
        """
        import ctypes
        import time

        import numpy as np

        def _fail(message: str, *, cancelled: bool = False) -> ProxyGenerationResult:
            try:
                if os.path.exists(proxy_path):
                    os.remove(proxy_path)
            except OSError:
                pass
            return ProxyGenerationResult(
                success=False,
                source_path=source_path,
                cancelled=cancelled,
                error_message=message,
                elapsed_seconds=time.time() - start_time,
            )

        try:
            import av
            from .topos_source import check_topos_codec_available
            from src.shared.codec.topos_binding import (
                TOPOS_CODEC_ABI_VERSION,
                TOPOS_MATRIX_CODES,
                TOPOS_PRIMARIES_CODES,
                TOPOS_TRANSFER_CODES,
                ToposCodec,
                ToposCodecError,
                ToposMuxFile,
                _CFrameConfig,
            )
            from src.shared.codec.topos_profiles import get_topos_tier
        except ImportError as e:
            return _fail(f"Topos proxy chain unavailable: {e}")

        if not check_topos_codec_available():
            return _fail("Topos proxy: native topos codec 不可用")

        input_container = None
        mux = None
        frame_count = 0
        try:
            input_container = av.open(str(source_path))
            video_stream = next(
                (s for s in input_container.streams if s.type == "video"),
                None)
            if video_stream is None:
                return _fail("Topos proxy: source has no video stream")
            rate = video_stream.average_rate or video_stream.base_rate or 24
            num = max(1, int(round(float(rate) * 1000)))
            den = 1000
            ctx = video_stream.codec_context
            src_w = int(ctx.width or 0)
            src_h = int(ctx.height or 0)
            if src_w <= 0 or src_h <= 0:
                return _fail("Topos proxy: source dimensions unknown")
            # 等比缩小到目标框内并取偶（4:2:2 色度半宽，宽度必须为偶）
            out_w, out_h = self._fit_even(src_w, src_h, target_size)

            warned_labels: set = set()

            def _map_color(table: dict, value: object, default: int) -> int:
                """PyAV 色彩元数据 → topos 色彩码。

                G7：未知标签回退默认时显式告警（R-13 口径）——静默回退
                code 1=709 会把 PQ/HLG 源代理错标（代理可再生，故警告而非
                失败；每个未知值只警一次）。"""
                text = str(value).strip().lower() if value is not None else ''
                for candidate in (text, text.rsplit('_', 1)[-1]):
                    if candidate in table:
                        return table[candidate]
                if text and text not in warned_labels:
                    warned_labels.add(text)
                    logger.warning(
                        "Topos proxy: 未知色彩标签 %r 回退默认码 %s "
                        "（HDR 源请确认代理色彩标签）", text, default)
                return default

            primaries = _map_color(
                TOPOS_PRIMARIES_CODES, ctx.color_primaries, 1)
            # PyAV 命名：transfer=color_trc、matrix=colorspace
            transfer = _map_color(
                TOPOS_TRANSFER_CODES, ctx.color_trc, 1)
            matrix = _map_color(TOPOS_MATRIX_CODES, ctx.colorspace, 1)

            # AUD-35603：色彩量程不得硬编码 limited——全量程源（yuvj/
            # JPEG 系）被错标后播放侧按 16-235 解码 0-255 数据，预览
            # 灰蒙/对比度失真。从流级 codec_context 解析并如实写入标签
            # （与 _run_topos 的 range_code 口径一致：1=full，0=limited；
            # 播放侧 ToposMediaSource 读 movie 级 summary.color_range，
            # 故以 movie_cfg 为权威）。swscale reformat 不传量程参数，
            # 像素数据保持源量程——标签如实即正确。无法判定时按
            # limited 回退并告警一次（G7 口径，代理可再生）。
            # AVColorRange：0=unspecified, 1=MPEG(limited), 2=JPEG(full)。
            stream_range = int(getattr(ctx, 'color_range', 0) or 0)
            if stream_range == 2:
                range_code = 1
            elif stream_range == 1:
                range_code = 0
            else:
                range_code = 0
                if 'color_range:unspecified' not in warned_labels:
                    warned_labels.add('color_range:unspecified')
                    logger.warning(
                        "Topos proxy: 源色彩量程未声明，按 limited 回退"
                        "（full-range 源的代理预览可能偏灰，请确认源标签）")

            proxy_qp = 34  # sized 编码不可用时的固定 QP 兜底
            codec = ToposCodec()
            movie_cfg = codec.movie_config(
                out_w, out_h,
                qp=proxy_qp, qmatrix=1,
                alpha_mode=0,
                alpha_bit_depth=0,
                alpha_premultiplied=False,
                color_range=range_code,
                color_primaries=primaries,
                color_transfer=transfer,
                color_matrix=matrix,
                timescale=num,
            )

            fc = _CFrameConfig()
            fc.struct_size = ctypes.sizeof(_CFrameConfig)
            fc.abi_version = TOPOS_CODEC_ABI_VERSION
            fc.visible_width, fc.visible_height = out_w, out_h
            fc.qp_base = proxy_qp
            fc.qmatrix_id = 1
            fc.alpha_mode = 0
            fc.alpha_bit_depth = 0
            fc.alpha_premultiplied = 0
            fc.color_range = range_code
            fc.color_primaries = primaries
            fc.color_transfer = transfer
            fc.color_matrix = matrix
            fc.sar_num, fc.sar_den = 1, 1
            # V 代际收纳（D3）：显式钉 V7-R2（cfg em=8），与 _run_topos /
            # ToposVideoEncoder 缺省口径一致，不依赖零值语义。
            fc.reserved[0] = 8

            proxy_tier = get_topos_tier('proxy')
            target_bytes = (
                proxy_tier.frame_target_bytes(out_w, out_h)
                if proxy_tier.is_rate_controlled else None)

            mux = ToposMuxFile(codec, proxy_path, movie_cfg)
            last_progress_at = 0.0
            total_estimate = (
                int(total_frames) if total_frames
                else max(1, int(float(rate) * 24.0)))
            for packet in input_container.demux(video_stream):
                if self._cancelled:
                    mux.close()
                    mux = None
                    return _fail(
                        "Generation cancelled by user", cancelled=True)
                # AUD-35609：进程内编码的协作式暂停检查点
                self._wait_if_paused()
                if packet.stream.type != "video":
                    continue
                for decoded_frame in packet.decode():
                    frame = decoded_frame.reformat(
                        width=out_w, height=out_h, format="yuv422p10le")
                    planes = []
                    for plane in frame.planes[:3]:
                        # VideoPlane 走 buffer 协议手动构数组（PyAV 无
                        # plane.to_ndarray）；line_size 含行尾 padding，
                        # 切宽后再紧化——topos 编码面期望无 padding 的
                        # '<u2' (h, w) 平面（与 ToposMediaSource 输出一致）
                        line_u16 = plane.line_size // 2
                        arr = np.frombuffer(
                            bytes(plane), dtype='<u2').reshape(
                            plane.height, line_u16)[:, :plane.width]
                        # G7：binding 支持指针直传（_run_topos 同款）——
                        # 去掉每帧 3× 全帧 tobytes() 序列化拷贝
                        planes.append(np.ascontiguousarray(arr))
                    if target_bytes is not None:
                        pkt, _st, _qp = codec.encode_sized(
                            fc, planes, target_bytes, qp_min=0, qp_max=63)
                    else:
                        pkt, _st = codec.encode_frame(fc, planes)
                    mux.add_packet(pkt, pts=frame_count * den, dur=den)
                    frame_count += 1
                    now = time.time()
                    if progress_callback and now - last_progress_at >= 0.5:
                        last_progress_at = now
                        pct = 10.0 + 85.0 * min(1.0, frame_count / total_estimate)
                        progress_callback(ProxyProgress(
                            percent=pct,
                            current_frame=frame_count,
                            total_frames=int(total_frames or 0),
                            status_message="Encoding Topos proxy...",
                        ))
            mux.finish()
            mux.close()
            mux = None

            if frame_count == 0:
                return _fail("Topos proxy: source decoded 0 video frames")

            proxy_size = os.path.getsize(proxy_path) if os.path.exists(proxy_path) else 0
            return ProxyGenerationResult(
                success=True,
                source_path=source_path,
                proxy_path=proxy_path,
                proxy_size_bytes=proxy_size,
                proxy_resolution=(out_w, out_h),
                elapsed_seconds=time.time() - start_time,
            )
        except ToposCodecError as e:
            return _fail(f"Topos proxy encode failed: {e}")
        except Exception as e:
            logger.exception(f"Topos proxy generation failed for {source_path}")
            return _fail(f"Topos proxy generation failed: {e}")
        finally:
            if mux is not None:
                try:
                    mux.close()
                except Exception:
                    pass
            if input_container is not None:
                try:
                    input_container.close()
                except Exception:
                    pass

    def _probe_source(
        self,
        source_path: str
    ) -> Optional[tuple[int, int, float, float, int]]:
        """
        探测源文件信息（PyAV 优先，ffprobe CLI 兜底）。

        Args:
            source_path: 源文件路径

        Returns:
            (width, height, fps, duration, total_frames) 或 None
        """
        probed = self._probe_source_with_pyav(source_path)
        if probed is not None:
            return probed

        try:
            ffprobe_bin = find_ffprobe()
            if ffprobe_bin is None:
                logger.debug("ffprobe binary not found; PyAV probe also failed")
                return None

            cmd = [
                ffprobe_bin,
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height,r_frame_rate,duration,nb_frames',
                '-of', 'csv=p=0',
                source_path,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.debug(f"ffprobe failed: {result.stderr}")
                return None

            output = result.stdout.strip()
            parts = output.split(',')

            if len(parts) < 5:
                logger.debug(f"Unexpected ffprobe output: {output}")
                return None

            width = int(parts[0])
            height = int(parts[1])

            # 解析帧率
            fps_str = parts[2]
            if '/' in fps_str:
                num, den = fps_str.split('/')
                fps = float(num) / float(den) if float(den) > 0 else 24.0
            else:
                fps = float(fps_str)

            duration = float(parts[3]) if parts[3] else 0.0
            total_frames = int(parts[4]) if parts[4] else 0

            # 如果没有 total_frames，从 duration 估算
            if total_frames <= 0 and duration > 0 and fps > 0:
                total_frames = int(duration * fps)

            logger.debug(
                f"Probed source: {width}x{height}, {fps:.2f}fps, "
                f"{duration:.2f}s, {total_frames} frames"
            )

            return (width, height, fps, duration, total_frames)

        except Exception as e:
            logger.debug(f"ffprobe unavailable: {e}")
            return None

    def _probe_source_with_pyav(
        self,
        source_path: str,
    ) -> Optional[tuple[int, int, float, float, int]]:
        """Probe media through PyAV when ffprobe is not installed."""
        try:
            import av

            container = av.open(source_path)
            try:
                stream = container.streams.video[0]
                width = int(stream.codec_context.width)
                height = int(stream.codec_context.height)
                rate = stream.average_rate or stream.base_rate
                fps = float(rate) if rate is not None else 24.0
                duration = float(container.duration / av.time_base) if container.duration else 0.0
                total_frames = int(stream.frames or 0)
                if total_frames <= 0 and duration > 0.0 and fps > 0.0:
                    total_frames = int(duration * fps)
                return width, height, fps, duration, total_frames
            finally:
                container.close()
        except Exception as e:
            logger.error(f"PyAV source probe failed: {e}")
            return None

    def _build_ffmpeg_command(
        self,
        source_path: str,
        output_path: str,
        target_size: tuple[int, int],
    ) -> list[str]:
        """
        构建 FFmpeg 命令。

        Args:
            source_path: 源文件路径
            output_path: 输出文件路径
            target_size: 目标分辨率

        Returns:
            FFmpeg 命令列表
        """
        cmd: list[str] = [
            self._ffmpeg_bin or find_ffmpeg() or 'ffmpeg',
            '-y',
            '-i', source_path,
        ]

        # 添加视频编码参数
        video_args = self.config.get_ffmpeg_args(target_size)
        cmd.extend(video_args)

        # 音频处理：对于代理，可以降质或去掉音频
        if self.config.format == ProxyFormat.IMAGE_SEQUENCE:
            # 图像序列不需要音频
            cmd.extend(['-an'])
        else:
            # 视频文件保留音频，但降低质量
            cmd.extend([
                '-c:a', 'aac',
                '-b:a', '64k',
                '-movflags', '+faststart',
            ])

        # 输出路径
        cmd.append(output_path)

        logger.debug(f"FFmpeg command: {' '.join(cmd)}")
        return cmd

    def _run_pyav(
        self,
        *,
        source_path: str,
        proxy_path: str,
        target_size: tuple[int, int],
        total_frames: int,
        start_time: float,
        progress_callback: Optional[Callable[[ProxyProgress], None]],
    ) -> ProxyGenerationResult:
        """Encode an edit proxy using bundled PyAV/FFmpeg libs (primary path).

        与 CLI 路径行为对齐：保持宽高比缩放（等比缩小到目标框内）、
        视频格式保留音频（AAC 64k）、mp4/mov 输出启用 faststart。
        """
        import time

        if self.config.format == ProxyFormat.IMAGE_SEQUENCE:
            return ProxyGenerationResult(
                success=False,
                source_path=source_path,
                error_message="PyAV path does not support image-sequence proxies",
                elapsed_seconds=time.time() - start_time,
            )

        input_container = None
        output_container = None
        frame_count = 0
        completed = False
        try:
            import av

            input_container = av.open(source_path)
            input_stream = input_container.streams.video[0]
            rate = input_stream.average_rate or input_stream.base_rate or 24
            codec_name = (
                "prores_ks"
                if self.config.format == ProxyFormat.PRORES_PROXY
                else "libx264"
            )
            pixel_format = (
                "yuv422p10le"
                if self.config.format == ProxyFormat.PRORES_PROXY
                else "yuv420p"
            )

            # 等比缩小到目标框内（对齐 CLI 的 force_original_aspect_ratio=decrease），
            # 尺寸取偶以兼容 yuv420p。
            src_w = int(input_stream.codec_context.width)
            src_h = int(input_stream.codec_context.height)
            out_w, out_h = self._fit_even(src_w, src_h, target_size)

            muxer_options = {"movflags": "+faststart"} if proxy_path.lower().endswith((".mp4", ".mov")) else {}
            output_container = av.open(proxy_path, mode="w", options=muxer_options)
            output_stream = output_container.add_stream(codec_name, rate=rate)
            output_stream.width, output_stream.height = out_w, out_h
            output_stream.pix_fmt = pixel_format
            if self.config.format == ProxyFormat.PRORES_PROXY:
                profile = {
                    "proxy": "proxy",
                    "422LT": "lt",
                    "422": "standard",
                    "422HQ": "hq",
                }.get(self.config.prores_profile, "proxy")
                output_stream.codec_context.options = {"profile": profile}
            else:
                output_stream.codec_context.options = {
                    "crf": str(self.config.h264_crf),
                    "g": "1",
                    "keyint_min": "1",
                    "sc_threshold": "0",
                }

            # 音频：与 CLI 的 `-c:a aac -b:a 64k` 对齐（无音频流则跳过）。
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
                        "aac", rate=int(audio_stream.rate or 48000),
                    )
                    out_audio_stream.bit_rate = 64000
                except Exception as audio_exc:
                    logger.warning(
                        "Proxy audio encode unavailable (%s); continuing video-only",
                        audio_exc,
                    )
                    audio_resampler = None
                    out_audio_stream = None

            def _encode_audio_frame(decoded_audio) -> None:
                if out_audio_stream is None or audio_resampler is None:
                    return
                for resampled in self._resample_audio(audio_resampler, decoded_audio):
                    for packet in out_audio_stream.encode(resampled):
                        output_container.mux(packet)

            # 一次 demux 同时分发视频/音频（先解完视频再 demux 音频会读到文件尾），
            # 且包顺序天然交错，利于 muxer。
            streams = [input_stream] if audio_stream is None else [input_stream, audio_stream]
            last_progress_at = 0.0
            for packet in input_container.demux(*streams):
                if self._cancelled:
                    return ProxyGenerationResult(
                        success=False,
                        source_path=source_path,
                        cancelled=True,
                        error_message="Generation cancelled by user",
                        elapsed_seconds=time.time() - start_time,
                    )
                # AUD-35609：进程内编码的协作式暂停检查点
                self._wait_if_paused()
                if packet.stream.type == "audio":
                    for decoded_audio in packet.decode():
                        _encode_audio_frame(decoded_audio)
                    continue
                if packet.stream.type != "video":
                    continue
                for decoded_frame in packet.decode():
                    proxy_frame = decoded_frame.reformat(
                        width=out_w,
                        height=out_h,
                        format=pixel_format,
                    )
                    for pkt in output_stream.encode(proxy_frame):
                        output_container.mux(pkt)
                    frame_count += 1

                    now = time.time()
                    if progress_callback and now - last_progress_at >= 0.5:
                        last_progress_at = now
                        elapsed = max(0.001, now - start_time)
                        current_fps = frame_count / elapsed
                        progress_callback(ProxyProgress(
                            percent=min(95.0, frame_count / max(1, total_frames) * 100.0),
                            current_frame=frame_count,
                            total_frames=total_frames,
                            fps=current_fps,
                            elapsed_seconds=elapsed,
                            estimated_remaining_seconds=(
                                max(0, total_frames - frame_count) / current_fps
                                if current_fps > 0.0 else 0.0
                            ),
                            status_message=f"Encoding frame {frame_count}/{total_frames}",
                        ))

            for packet in output_stream.encode():
                output_container.mux(packet)
            if out_audio_stream is not None:
                for packet in out_audio_stream.encode():
                    output_container.mux(packet)
            output_container.close()
            output_container = None
            input_container.close()
            input_container = None

            if not os.path.isfile(proxy_path):
                return ProxyGenerationResult(
                    success=False,
                    source_path=source_path,
                    error_message="PyAV did not create the proxy file",
                    elapsed_seconds=time.time() - start_time,
                )

            elapsed = time.time() - start_time
            if progress_callback:
                progress_callback(ProxyProgress(
                    percent=100.0,
                    current_frame=frame_count,
                    total_frames=total_frames,
                    status_message="Complete",
                ))
            completed = True
            return ProxyGenerationResult(
                success=True,
                source_path=source_path,
                proxy_path=proxy_path,
                proxy_size_bytes=os.path.getsize(proxy_path),
                proxy_resolution=(out_w, out_h),
                duration_seconds=frame_count / float(rate),
                elapsed_seconds=elapsed,
            )
        except Exception as e:
            logger.exception("PyAV proxy generation failed")
            return ProxyGenerationResult(
                success=False,
                source_path=source_path,
                error_message=f"PyAV proxy generation failed: {e}",
                elapsed_seconds=time.time() - start_time,
            )
        finally:
            if output_container is not None:
                try:
                    output_container.close()
                except Exception:
                    pass
            if input_container is not None:
                try:
                    input_container.close()
                except Exception:
                    pass
            if not completed and os.path.isfile(proxy_path):
                try:
                    os.remove(proxy_path)
                except OSError:
                    pass

    @staticmethod
    def _fit_even(
        src_w: int, src_h: int, target_size: tuple[int, int],
    ) -> tuple[int, int]:
        """等比缩放到目标框内并取偶（对齐 CLI scale 的 decrease 语义）。"""
        target_w, target_h = target_size
        if src_w <= 0 or src_h <= 0:
            return target_w, target_h
        scale = min(target_w / src_w, target_h / src_h, 1.0)
        fitted_w = max(2, int(src_w * scale) // 2 * 2)
        fitted_h = max(2, int(src_h * scale) // 2 * 2)
        return fitted_w, fitted_h

    @staticmethod
    def _resample_audio(resampler, frame) -> list:
        """兼容不同 PyAV 版本的 resample 返回值（list / 单帧 / None）。"""
        result = resampler.resample(frame)
        if result is None:
            return []
        if isinstance(result, list):
            return result
        return [result]

    def _run_ffmpeg(
        self,
        ffmpeg_cmd: list[str],
        source_path: str,
        proxy_path: str,
        total_frames: int,
        start_time: float,
        progress_callback: Optional[Callable[[ProxyProgress], None]],
        source_fps: Optional[float] = None,
    ) -> ProxyGenerationResult:
        """
        执行 FFmpeg 编码。

        Args:
            ffmpeg_cmd: FFmpeg 命令
            source_path: 源文件路径
            proxy_path: 代理文件路径
            total_frames: 总帧数
            start_time: 开始时间
            progress_callback: 进度回调
            source_fps: generate() 入口探测到的源帧率（AUD-35600：成功
                路径复用该值计算时长，不再重复探测源文件）

        Returns:
            ProxyGenerationResult
        """
        import time

        try:
            with self._lock:
                self._process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            process = self._process
            if process is None:
                return ProxyGenerationResult(
                    success=False,
                    source_path=source_path,
                    error_message="Failed to start FFmpeg process",
                )

            # 读取 stderr 监控进度
            elapsed = 0.0
            last_progress_update = 0.0
            frame_count = 0

            # 读取 stderr（FFmpeg 输出进度信息到这里）
            # AUD-35601/35602：ffmpeg 的 stats 更新以 \r 结尾（readline
            # 按 \n 分行会在编码期间整段阻塞，帧数解析与取消轮询停摆），
            # 且 Windows 的 select 不能用于管道（stderr 永不排水，管道
            # 写满后 ffmpeg 阻塞死锁）。统一改为后台排水线程按块读取，
            # 监控循环从缓冲区按 \r/\n 切行解析。
            stderr_chunks: list[bytes] = []
            stderr_lock = threading.Lock()

            def _drain_stderr() -> None:
                assert process.stderr is not None
                try:
                    while True:
                        chunk = process.stderr.read(4096)
                        if not chunk:
                            break
                        with stderr_lock:
                            stderr_chunks.append(chunk)
                except (OSError, ValueError) as exc:
                    # 进程被 kill/关闭后 read 可能抛出——排水线程安静退出
                    logger.debug(f"FFmpeg stderr drain thread ended: {exc}")

            drain_thread = threading.Thread(
                target=_drain_stderr,
                daemon=True,
                name="ProxyFFmpegStderr",
            )
            drain_thread.start()

            stderr_lines: list[str] = []
            pending = b""
            while True:
                if self._cancelled:
                    process.kill()
                    process.wait(timeout=5)
                    return ProxyGenerationResult(
                        success=False,
                        source_path=source_path,
                        cancelled=True,
                        elapsed_seconds=time.time() - start_time,
                        error_message="Generation cancelled by user",
                    )

                # 检查进程是否结束
                retcode = process.poll()
                if retcode is not None:
                    # 进程退出后等排水线程读完管道内残余数据
                    drain_thread.join(timeout=5)
                    with stderr_lock:
                        pending += b"".join(stderr_chunks)
                        stderr_chunks.clear()
                    for line in pending.replace(b'\r', b'\n').split(b'\n'):
                        if line:
                            stderr_lines.append(
                                line.decode('utf-8', errors='replace'))
                    pending = b""
                    break

                # 取出排水线程新写入的字节并按 \r/\n 切行
                with stderr_lock:
                    new_data = b"".join(stderr_chunks)
                    stderr_chunks.clear()
                if new_data:
                    pending += new_data
                    complete_lines = pending.replace(b'\r', b'\n').split(b'\n')
                    pending = complete_lines.pop()  # 最后一段可能不完整，留待下轮
                    for raw_line in complete_lines:
                        if raw_line:
                            line = raw_line.decode('utf-8', errors='replace')
                            stderr_lines.append(line)
                            # 解析帧数
                            if 'frame=' in line:
                                try:
                                    frame_part = line.split('frame=')[1].split()[0]
                                    frame_count = int(frame_part)
                                except (IndexError, ValueError):
                                    pass

                # 更新进度
                elapsed = time.time() - start_time
                if elapsed - last_progress_update >= 0.5 and progress_callback:
                    last_progress_update = elapsed
                    percent = min(
                        95.0,
                        (frame_count / total_frames * 100.0) if total_frames > 0 else 50.0
                    )
                    current_fps = frame_count / elapsed if elapsed > 0 else 0.0
                    remaining = (
                        (total_frames - frame_count) / current_fps
                        if current_fps > 0 else 0.0
                    )

                    progress_callback(ProxyProgress(
                        percent=percent,
                        current_frame=frame_count,
                        total_frames=total_frames,
                        fps=current_fps,
                        elapsed_seconds=elapsed,
                        estimated_remaining_seconds=remaining,
                        status_message=f"Encoding frame {frame_count}/{total_frames}",
                    ))

                # 轮询间隔（替代原 select 的 0.1s 超时，避免空转占满 CPU）
                time.sleep(0.1)

            # 等待进程结束
            process.wait(timeout=30)

            # 检查是否成功
            if process.returncode != 0:
                # stderr 已由排水线程收齐（break 前已 join 并合并残余块）
                full_stderr = '\n'.join(stderr_lines)
                stderr_tail_lines = full_stderr.strip().splitlines()[-20:]
                stderr_tail = '\n'.join(stderr_tail_lines)
                logger.error(
                    "FFmpeg failed with code %s:\n%s",
                    process.returncode,
                    stderr_tail,
                )
                error_detail = stderr_tail_lines[-1] if stderr_tail_lines else "Unknown FFmpeg error"

                return ProxyGenerationResult(
                    success=False,
                    source_path=source_path,
                    error_message=(
                        f"FFmpeg exited with code {process.returncode}: {error_detail}"
                    ),
                    elapsed_seconds=time.time() - start_time,
                )

            # 验证输出文件
            if not os.path.isfile(proxy_path):
                return ProxyGenerationResult(
                    success=False,
                    source_path=source_path,
                    error_message="Proxy file was not created",
                    elapsed_seconds=time.time() - start_time,
                )

            proxy_size = os.path.getsize(proxy_path)
            elapsed = time.time() - start_time

            # 获取代理分辨率
            proxy_size_result = self._probe_source(proxy_path)
            proxy_res = (0, 0)
            if proxy_size_result:
                proxy_res = (proxy_size_result[0], proxy_size_result[1])

            # 最终进度更新
            if progress_callback:
                progress_callback(ProxyProgress(
                    percent=100.0,
                    current_frame=total_frames,
                    total_frames=total_frames,
                    status_message="Complete",
                ))

            logger.info(
                f"Proxy generated: {proxy_path} ({proxy_size / 1024 / 1024:.1f}MB, "
                f"{proxy_res[0]}x{proxy_res[1]}, {elapsed:.1f}s)"
            )

            # AUD-35600：复用 generate() 入口探测到的 fps，不再对源文件
            # 重复两次 _probe_source；fps 缺失/非正时降级为 0.0 而非抛
            # ZeroDivisionError/TypeError 把成功误报为失败。
            duration_seconds = (
                total_frames / source_fps
                if source_fps is not None and source_fps > 0 else 0.0)

            return ProxyGenerationResult(
                success=True,
                source_path=source_path,
                proxy_path=proxy_path,
                proxy_size_bytes=proxy_size,
                proxy_resolution=proxy_res,
                duration_seconds=duration_seconds,
                elapsed_seconds=elapsed,
            )

        except subprocess.TimeoutExpired:
            if self._process:
                self._process.kill()
            return ProxyGenerationResult(
                success=False,
                source_path=source_path,
                error_message="FFmpeg timed out",
                elapsed_seconds=time.time() - start_time,
            )
        except Exception as e:
            logger.exception("FFmpeg execution failed")
            return ProxyGenerationResult(
                success=False,
                source_path=source_path,
                error_message=str(e),
                elapsed_seconds=time.time() - start_time,
            )
        finally:
            with self._lock:
                self._process = None
