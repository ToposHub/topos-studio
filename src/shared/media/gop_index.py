"""
持久化 GOP / keyframe 索引（性能计划 P2-05）。

职责：
- 扫描一次容器 packet（不解码），得到 keyframe 帧号表并持久化到平台缓存目录。
- 为 seek 决策层提供真实 keyframe 间隔（此前 ``seek.py`` 依赖硬编码猜测表，
  与真实素材（如 4K HEVC keyint=25）不符）。
- 为 GOP seek 提供精确 keyframe 定位与 decode-forward 距离上界，避免病态
  长距离 walk。

存储布局（复用 ProxyManifest 的失效模式）：
    <platform_cache_dir>/gop_index/<md5(path|mtime_ns|size)>.json

设计原则：
- 纯函数式 API，绝不抛异常到调用方（索引失败一律回退到无索引路径）。
- 身份键含 mtime_ns/size：源文件变化自动失效，无需显式清理。
- 不在模块内起线程：后台构建策略由调用方决定（FFmpegMediaSource 在首次
  GOP seek 时后台构建，构建完成前的 seek 走原路径）。

已知限制：
- keyframe 帧号按 CFR（恒定帧率）由 pts 推导，与 FFmpegMediaSource 的
  _frame_to_pts 同一假设；VFR 素材的帧号表为近似值（pts 表仍然精确，
  seek 以 pts 为准）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 索引 schema 版本：结构不兼容时自动重建
GOP_INDEX_SCHEMA = 1

# 超过该大小的文件不做同步自动构建（demux 全量扫描的 IO 预算护栏）；
# 显式调用 build_gop_index() 仍可强制构建。
GOP_INDEX_AUTO_BUILD_MAX_BYTES = 8 * 1024 * 1024 * 1024

# 保存互斥锁（同一路径并发构建时避免交错写；写入本身是原子 rename）
_save_lock = threading.Lock()


@dataclass(frozen=True)
class GopIndexKey:
    """
    GOP 索引身份键（复用 ProxyManifest 失效模式）。

    Attributes:
        source_path: 源文件绝对路径
        source_mtime_ns: 源文件 mtime（纳秒）
        source_size: 源文件大小（字节）
    """

    source_path: str
    source_mtime_ns: int
    source_size: int

    def to_hash(self) -> str:
        """生成稳定 hash（磁盘文件名 + 快速比较）。"""
        raw = "|".join([
            self.source_path,
            str(self.source_mtime_ns),
            str(self.source_size),
        ])
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @classmethod
    def from_path(cls, path: str | os.PathLike) -> Optional["GopIndexKey"]:
        """从文件路径构建身份键；文件不存在返回 None。"""
        try:
            st = os.stat(path)
        except OSError:
            return None
        return cls(
            source_path=os.path.abspath(os.fspath(path)),
            source_mtime_ns=st.st_mtime_ns,
            source_size=st.st_size,
        )


@dataclass
class GopIndex:
    """
    GOP / keyframe 索引数据。

    Attributes:
        codec_name: 视频编解码器名（如 "hevc"）
        frame_count: 总帧数（由 keyframe 表与流信息推导，可能小于实际值）
        fps: 构建时的帧率（Rational）
        time_base: 构建时的流 time_base（Rational）
        keyframe_frames: keyframe 帧号表（升序，CFR 推导）
        schema: 索引结构版本
        identity: 来源身份键（用于加载时校验）
    """

    codec_name: str
    frame_count: int
    fps: Fraction
    time_base: Fraction
    keyframe_frames: tuple[int, ...]
    schema: int = GOP_INDEX_SCHEMA
    identity: Optional[GopIndexKey] = field(default=None, repr=False)

    @property
    def avg_gop(self) -> float:
        """平均 keyframe 间隔（帧）；不足 2 个 keyframe 时返回 0。"""
        if len(self.keyframe_frames) < 2:
            return 0.0
        gaps = [
            b - a for a, b in zip(self.keyframe_frames, self.keyframe_frames[1:])
        ]
        return sum(gaps) / len(gaps)

    @property
    def keyframe_interval(self) -> int:
        """keyframe 间隔的保守上界（取最大 GOP，供决策层使用）。"""
        if len(self.keyframe_frames) < 2:
            return max(1, self.frame_count)
        gaps = [
            b - a for a, b in zip(self.keyframe_frames, self.keyframe_frames[1:])
        ]
        return max(1, max(gaps))

    def pts_per_frame(self) -> Fraction:
        """每帧 PTS 增量（time_base 单位）＝ (1/fps) / time_base。"""
        return Fraction(self.fps.denominator * self.time_base.denominator,
                        self.fps.numerator * self.time_base.numerator)

    def frame_to_pts(self, frame_index: int) -> int:
        """帧号转 PTS（与 FFmpegMediaSource._frame_to_pts 同一舍入规则）。"""
        return int(frame_index * self.pts_per_frame() + Fraction(1, 2))

    def keyframe_pts(self) -> tuple[int, ...]:
        """keyframe PTS 表（升序）。"""
        return tuple(self.frame_to_pts(f) for f in self.keyframe_frames)

    def keyframe_frame_before(self, target_frame: int) -> Optional[int]:
        """目标帧之前（含）最近的 keyframe 帧号；无则 None。"""
        if not self.keyframe_frames:
            return None
        result: Optional[int] = None
        for kf in self.keyframe_frames:
            if kf <= target_frame:
                result = kf
            else:
                break
        return result

    def decode_distance(self, target_frame: int) -> Optional[int]:
        """
        seek 到 target_frame 需要的 decode-forward 帧数（keyframe 到目标）。

        Returns:
            距离（帧数），无 keyframe 表时 None。
        """
        kf = self.keyframe_frame_before(target_frame)
        if kf is None:
            return None
        return target_frame - kf

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容 dict。"""
        return {
            "schema": self.schema,
            "codec_name": self.codec_name,
            "frame_count": self.frame_count,
            "fps_num": self.fps.numerator,
            "fps_den": self.fps.denominator,
            "tb_num": self.time_base.numerator,
            "tb_den": self.time_base.denominator,
            "keyframe_frames": list(self.keyframe_frames),
            "identity": (
                {
                    "source_path": self.identity.source_path,
                    "source_mtime_ns": self.identity.source_mtime_ns,
                    "source_size": self.identity.source_size,
                }
                if self.identity is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["GopIndex"]:
        """反序列化；结构不兼容返回 None。"""
        try:
            if int(raw.get("schema", -1)) != GOP_INDEX_SCHEMA:
                return None
            identity_raw = raw.get("identity")
            identity = (
                GopIndexKey(
                    source_path=str(identity_raw["source_path"]),
                    source_mtime_ns=int(identity_raw["source_mtime_ns"]),
                    source_size=int(identity_raw["source_size"]),
                )
                if isinstance(identity_raw, dict)
                else None
            )
            keyframes = tuple(int(f) for f in raw["keyframe_frames"])
            if keyframes != tuple(sorted(keyframes)):
                return None
            return cls(
                codec_name=str(raw["codec_name"]),
                frame_count=int(raw["frame_count"]),
                fps=Fraction(int(raw["fps_num"]), int(raw["fps_den"])),
                time_base=Fraction(int(raw["tb_num"]), int(raw["tb_den"])),
                keyframe_frames=keyframes,
                schema=int(raw["schema"]),
                identity=identity,
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None


# =============================================================================
# 构建 / 持久化
# =============================================================================


def build_gop_index(
    path: str | os.PathLike,
    *,
    stream_index: int = 0,
    identity: Optional[GopIndexKey] = None,
) -> Optional[GopIndex]:
    """
    扫描容器 packet 构建 GOP 索引（不解码，只读 packet 头）。

    Args:
        path: 媒体文件路径
        stream_index: 视频流序号（默认第一条视频流）
        identity: 显式身份键；缺省时从文件 stat 构建

    Returns:
        GopIndex；打开失败或无视频流时 None。
    """
    try:
        import av
    except ImportError:
        return None

    if identity is None:
        identity = GopIndexKey.from_path(path)
    if identity is None:
        return None

    try:
        with av.open(os.fspath(path)) as container:
            video_streams = container.streams.video
            if not video_streams:
                return None
            stream = video_streams[min(stream_index, len(video_streams) - 1)]
            tb = Fraction(stream.time_base.numerator, stream.time_base.denominator)
            avg_rate = stream.average_rate
            if avg_rate is None or avg_rate <= 0:
                return None
            fps = Fraction(avg_rate.numerator, avg_rate.denominator)
            # 每帧 ticks = (1/fps) / time_base
            ppf = Fraction(fps.denominator * tb.denominator,
                           fps.numerator * tb.numerator)

            keyframe_frames: list[int] = []
            last_frame = 0
            for pkt in container.demux(stream):
                pts = pkt.pts if pkt.pts is not None else pkt.dts
                if pts is None:
                    continue
                frame = int(pts / ppf + Fraction(1, 2))
                if frame < 0:
                    continue
                last_frame = max(last_frame, frame)
                if pkt.is_keyframe:
                    # 容器可能对同一 keyframe 发出多个 packet（如伴随的
                    # side-data 包）；只保留升序去重后的表。
                    if not keyframe_frames or frame > keyframe_frames[-1]:
                        keyframe_frames.append(frame)

            if not keyframe_frames:
                return None
            return GopIndex(
                codec_name=str(stream.codec_context.name or ""),
                frame_count=last_frame + 1,
                fps=fps,
                time_base=tb,
                keyframe_frames=tuple(keyframe_frames),
                identity=identity,
            )
    except Exception as e:  # noqa: BLE001 - 索引构建绝不向调用方抛异常
        logger.debug("gop index build failed for %s: %s", path, e)
        return None


def cache_file_for(key: GopIndexKey) -> Path:
    """索引缓存文件路径：<platform_cache_dir>/gop_index/<md5>.json。"""
    from src.shared.config.config_manager import ConfigManager

    return Path(ConfigManager.get_platform_cache_dir()) / "gop_index" / f"{key.to_hash()}.json"


def save_gop_index(index: GopIndex, key: GopIndexKey) -> bool:
    """
    原子写入索引缓存文件。

    Returns:
        写入成功返回 True；失败（目录不可写等）返回 False。
    """
    if index.identity is None:
        index.identity = key
    payload = index.to_dict()
    # 身份字段冗余进内容，加载时可二次校验
    payload.setdefault("identity", {
        "source_path": key.source_path,
        "source_mtime_ns": key.source_mtime_ns,
        "source_size": key.source_size,
    })
    try:
        target = cache_file_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _save_lock:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(target.parent), prefix=".gopidx-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                os.replace(tmp_name, target)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        return True
    except OSError as e:
        logger.debug("gop index save failed for %s: %s", key.source_path, e)
        return False


def load_gop_index(
    path: str | os.PathLike,
    *,
    key: Optional[GopIndexKey] = None,
) -> Optional[GopIndex]:
    """
    加载持久化索引；文件缺失、损坏或身份不匹配（mtime/size 变化）返回 None。
    """
    if key is None:
        key = GopIndexKey.from_path(path)
    if key is None:
        return None
    cache_file = cache_file_for(key)
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    index = GopIndex.from_dict(raw)
    if index is None:
        return None
    # 身份校验：源文件变化 → 索引失效
    if index.identity != key:
        return None
    return index


def get_or_build_gop_index(
    path: str | os.PathLike,
    *,
    force: bool = False,
    max_bytes: int = GOP_INDEX_AUTO_BUILD_MAX_BYTES,
) -> Optional[GopIndex]:
    """
    查询持久化索引，未命中时同步构建并落盘。

    这是「读穿」入口：调用方（解码工作线程）首次 GOP seek 时调用一次，
    之后命中内存/磁盘缓存。

    Args:
        path: 媒体文件路径
        force: True 时跳过缓存直接重建
        max_bytes: 自动构建的文件大小上界（默认 8GB；超大文件同步扫描
                   的 IO 成本过高，返回 None 走无索引路径）

    Returns:
        GopIndex；任何失败返回 None（绝不抛异常）。
    """
    key = GopIndexKey.from_path(path)
    if key is None:
        return None
    if not force:
        cached = load_gop_index(path, key=key)
        if cached is not None:
            return cached
    if key.source_size > max_bytes:
        return None
    index = build_gop_index(path, identity=key)
    if index is not None:
        save_gop_index(index, key)
    return index
