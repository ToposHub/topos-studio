"""P1-13 Consolidate —— 按剪辑使用范围裁剪收集工程媒体。

与 ``project_archive.collect_project``（整文件收集）互补：Consolidate 只
收集时间线实际使用的源区间（±handles），产出更小的自洽工程包：

- **remux（流复制，默认）**：ffmpeg ``-c copy``，零质量损失；起点对齐到
  区间内最后一个关键帧（GOP 对齐），clip 的 source_start 按对齐偏移精确
  重映射。精度受容器包粒度限制（视频=关键帧、音频≈一个 packet ≈20ms），
  handles 默认 12 帧已覆盖。
- **transcode（转码）**：视频重编码 Apple ProRes 422（prores_ks
  profile 3, yuv422p10le），帧精确、无 GOP 对齐；音频流复制。适合把
  长 GOP 素材转成剪辑友好母版再归档。

一个素材的多个使用区间合并后每段生成独立分段文件与独立素材（asset），
工程中引用该素材的 clip 全部重映射到所属分段（asset_id + source_start
偏移），原始素材从新工程移除、箱内引用同步替换。图片素材无时间轴概念，
按整文件复制（偏移 0）。虚拟素材（comp/nested/solid）不参与。

约束：纯核心模块，不依赖 Qt；工程落盘由调用方用 EditProjectIO 完成。
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from src.features.edit.core.asset_usage_index import build_asset_usage_index
from src.features.edit.core.project import EditProject, MediaAsset

logger = logging.getLogger(__name__)

CONSOLIDATE_REPORT_SCHEMA = "topos.consolidate.v1"

#: 分段生成模式
MODE_REMUX = "remux"
MODE_TRANSCODE = "transcode"
CONSOLIDATE_MODES = (MODE_REMUX, MODE_TRANSCODE)

#: 分段状态
STATUS_CONSOLIDATED = "consolidated"
STATUS_MISSING = "missing"
STATUS_FAILED = "failed"
STATUS_COPIED_IMAGE = "copied_image"  # 图片整文件复制（无时间轴可裁）

_DEFAULT_FALLBACK_FPS = 24.0
#: transcode 模式输出容器（ProRes 的标准载体）
_TRANSCODE_EXT = ".mov"
#: AUD-16311：单段 ffmpeg 默认超时（秒）。此前 None 表示不限时，UI 线程
#: 同步调用时一个挂死的 ffmpeg 会让界面永久冻结；30 分钟覆盖 ProRes
#: 转码长分段，同时给失控进程一个有界上限。显式传 ffmpeg_timeout 仍可覆盖。
_DEFAULT_FFMPEG_TIMEOUT_S = 1800.0


class ConsolidateError(RuntimeError):
    """Consolidate 过程中的可预期失败（ffmpeg 不可用/执行失败等）。"""


class ConsolidateCancelled(RuntimeError):
    """AUD-16311：调用方经 ``cancel_check`` 请求取消。

    在分段边界抛出——已成功的分段文件/报告状态保持一致（与单段失败的
    失败隔离策略相同），调用方捕获本异常后自行决定是否清理归档目录。
    """


@dataclass(frozen=True)
class ConsolidateProgress:
    """AUD-16311：分段级进度快照（经 ``progress_cb`` 回调上报）。

    Attributes:
        phase: 进度阶段——``"segment"``（开始处理一段）/ ``"done"``（全部完成）。
        asset_id: 当前处理的原始素材 ID。
        display_name: 素材显示名（UI 展示用）。
        segment_index: 当前分段序号（1-based）。
        segment_count: 该素材的分段总数。
        completed_segments: 全工程已完成分段数。
        total_segments: 全工程分段总数（含图片整份复制）。
    """

    phase: str
    asset_id: str
    display_name: str
    segment_index: int
    segment_count: int
    completed_segments: int
    total_segments: int


@dataclass(frozen=True)
class SourceRange:
    """素材源帧区间 [start, end)，单位为源媒体帧。"""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"Invalid SourceRange: [{self.start}, {self.end})")


@dataclass(frozen=True)
class _SegmentPlan:
    """单个分段的执行计划（对齐后）。"""

    asset_id: str
    index: int  # 1-based
    start_frame: int  # 对齐后的分段起始源帧（新文件的第 0 帧）
    end_frame: int  # 请求的区间末（不含）
    start_sec: float  # 对齐后起始秒
    duration_sec: float  # ≥ 请求时长（关键帧对齐可能略向前扩）
    out_name: str


@dataclass(frozen=True)
class ConsolidateSegmentRecord:
    """单个素材分段的收集结果。"""

    asset_id: str
    display_name: str
    source_path: str
    segment_index: int
    mode: str
    requested_start_frame: int
    requested_end_frame: int
    aligned_start_frame: int
    new_asset_id: str = ""
    archived_path: str = ""  # 相对归档根目录；缺失/失败为空
    status: str = ""
    sha256: str = ""
    file_size: int = 0
    source_file_size: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "display_name": self.display_name,
            "source_path": self.source_path,
            "segment_index": self.segment_index,
            "mode": self.mode,
            "requested_start_frame": self.requested_start_frame,
            "requested_end_frame": self.requested_end_frame,
            "aligned_start_frame": self.aligned_start_frame,
            "new_asset_id": self.new_asset_id,
            "archived_path": self.archived_path,
            "status": self.status,
            "sha256": self.sha256,
            "file_size": self.file_size,
            "source_file_size": self.source_file_size,
        }


@dataclass
class ConsolidateReport:
    """Consolidate 报告。"""

    archive_root: str = ""
    mode: str = MODE_REMUX
    handles_frames: int = 0
    records: Tuple[ConsolidateSegmentRecord, ...] = ()
    # 缺失素材 → 全部时间线使用位置（与 collect 报告同源）
    missing_usage: Dict[str, Tuple[str, ...]] = dataclasses.field(default_factory=dict)
    skipped_unused: Tuple[str, ...] = ()
    skipped_virtual: Tuple[str, ...] = ()
    # 重映射成功 clip_id → "分段路径 @ 新source_start"
    remapped_clips: Dict[str, str] = dataclasses.field(default_factory=dict)
    unmapped_clip_ids: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def consolidated(self) -> Tuple[ConsolidateSegmentRecord, ...]:
        return tuple(
            r for r in self.records
            if r.status in (STATUS_CONSOLIDATED, STATUS_COPIED_IMAGE)
        )

    @property
    def failures(self) -> Tuple[ConsolidateSegmentRecord, ...]:
        return tuple(r for r in self.records if r.status == STATUS_FAILED)

    @property
    def missing(self) -> Tuple[ConsolidateSegmentRecord, ...]:
        return tuple(r for r in self.records if r.status == STATUS_MISSING)

    @property
    def passed(self) -> bool:
        """成功 = 无失败分段、无未映射 clip（缺失素材允许，见 missing_usage）。"""
        return not self.failures and not self.unmapped_clip_ids

    @property
    def source_bytes(self) -> int:
        """被裁剪的原始文件总大小（按素材去重）。"""
        seen: Dict[str, int] = {}
        for r in self.records:
            if r.source_file_size:
                seen[r.source_path] = r.source_file_size
        return sum(seen.values())

    @property
    def segment_bytes(self) -> int:
        return sum(r.file_size for r in self.consolidated)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": CONSOLIDATE_REPORT_SCHEMA,
            "archive_root": self.archive_root,
            "mode": self.mode,
            "handles_frames": self.handles_frames,
            "records": [r.to_dict() for r in self.records],
            "missing_usage": {
                asset_id: list(locs)
                for asset_id, locs in self.missing_usage.items()
            },
            "skipped_unused": list(self.skipped_unused),
            "skipped_virtual": list(self.skipped_virtual),
            "remapped_clips": dict(self.remapped_clips),
            "unmapped_clip_ids": list(self.unmapped_clip_ids),
            "warnings": list(self.warnings),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 使用范围计算
# ─────────────────────────────────────────────────────────────────────────────

def compute_used_ranges(
    sequences: Iterable[Any],
    handles_frames: int = 0,
) -> Dict[str, Tuple[SourceRange, ...]]:
    """从序列的全部视频/音频轨道 clip 提取每个素材的源帧使用区间并合并。

    Args:
        sequences: TimelineSequence 可迭代对象
        handles_frames: 每个区间前后各扩展的手柄帧数（下限裁到 0）

    Returns:
        asset_id → 合并后升序区间元组
    """
    raw: Dict[str, List[SourceRange]] = {}
    for seq in sequences:
        tracks = tuple(getattr(seq, "video_tracks", ())) + tuple(
            getattr(seq, "audio_tracks", ())
        )
        for track in tracks:
            for clip in getattr(track, "clips", ()):
                asset_id = getattr(clip, "asset_id", "")
                if not asset_id:
                    continue
                start = int(getattr(clip, "source_start", 0) or 0)
                duration = int(getattr(clip, "source_duration", 0) or 0)
                if duration <= 0:
                    continue
                end = start + duration
                if handles_frames > 0:
                    start = max(0, start - handles_frames)
                    end += handles_frames
                raw.setdefault(asset_id, []).append(SourceRange(start, end))

    merged: Dict[str, Tuple[SourceRange, ...]] = {}
    for asset_id, ranges in raw.items():
        merged[asset_id] = _merge_ranges(ranges)
    return merged


def _merge_ranges(ranges: Sequence[SourceRange]) -> Tuple[SourceRange, ...]:
    """合并重叠/相邻区间，返回升序不重叠元组。"""
    ordered = sorted(ranges, key=lambda r: r.start)
    out: List[SourceRange] = []
    for r in ordered:
        if out and r.start <= out[-1].end:
            if r.end > out[-1].end:
                out[-1] = SourceRange(out[-1].start, r.end)
        else:
            out.append(r)
    return tuple(out)


def _fps_float(value: Any, default: float = _DEFAULT_FALLBACK_FPS) -> float:
    """Rational/str/float 帧率统一转正 float。"""
    if value is None:
        return default
    if hasattr(value, "num") and hasattr(value, "den"):
        try:
            den = int(value.den)
            return int(value.num) / den if den else default
        except (TypeError, ValueError, ZeroDivisionError):
            return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f > 0.0 else default


def _asset_fps(project: EditProject, asset: MediaAsset, sequences: Sequence[Any]) -> float:
    """素材源帧率：优先探测元数据，缺省回落到首个序列帧率。"""
    fps = asset.fps
    if fps:
        return _fps_float(fps)
    for seq in sequences:
        return _fps_float(getattr(seq, "fps", None))
    return _DEFAULT_FALLBACK_FPS


# ─────────────────────────────────────────────────────────────────────────────
# 分段执行（关键帧对齐 + ffmpeg）
# ─────────────────────────────────────────────────────────────────────────────

def _sha256_of(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _last_keyframe_at_or_before(path: str, seconds: float) -> Optional[float]:
    """返回 ≤ seconds 的最后一个视频关键帧时间（秒）；无视频流返回 None。

    只 demux 不解码；到目标时间即停，不扫描全文件。
    """
    import av

    with av.open(path) as container:
        vstreams = container.streams.video
        if not vstreams:
            return None
        stream = vstreams[0]
        tb = float(stream.time_base)
        last = 0.0
        for packet in container.demux(stream):
            if packet.dts is None:
                continue
            pts = packet.pts if packet.pts is not None else packet.dts
            t = pts * tb
            if t > seconds + 1e-6:
                break
            if packet.is_keyframe and t > last:
                last = t
        return last


def _run_ffmpeg(args: Sequence[str], timeout: Optional[float] = None) -> None:
    """执行 ffmpeg CLI（ffmpeg_locator：打包 bin/ → PATH），失败抛 ConsolidateError。"""
    from src.shared.ffmpeg_locator import find_ffmpeg

    ffmpeg_bin = find_ffmpeg()
    if ffmpeg_bin is None:
        raise ConsolidateError(
            "ffmpeg binary not found (bundled bin/ dir or PATH)"
        )
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise ConsolidateError(f"ffmpeg binary vanished: {ffmpeg_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConsolidateError(f"ffmpeg timed out: {' '.join(args[:6])} …") from exc
    if result.returncode != 0:
        tail = (result.stderr or "").strip()[-400:]
        raise ConsolidateError(f"ffmpeg failed ({result.returncode}): {tail}")


def _remux_segment(
    src: str, dst: str, start_sec: float, duration_sec: float,
    timeout: Optional[float] = None,
) -> None:
    """流复制分段。dst 扩展名决定容器（与源同容器，保证流兼容）。

    只映射视频/音频/字幕流：源容器中的 data 流（如 mp4 的 codec none
    轨道）无法写回 mp4，会让整个复用失败。

    AUD-16310：三个流选择器都带 ``?``（可选匹配）。纯音频素材两种模式
    都走本函数，``0:v`` 必失败；无音轨视频（航拍/录屏/代理）在 remux
    模式下 ``0:a`` 必失败——可选匹配让 ffmpeg 跳过缺失流而不是报错退出。
    """

    args = [
        "-ss", f"{start_sec:.6f}",
        "-i", src,
        "-t", f"{duration_sec:.6f}",
        "-map", "0:v?",
        "-map", "0:a?",
        "-map", "0:s?",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
    ]
    if os.path.splitext(dst)[1].lower() in (".mp4", ".mov", ".m4a"):
        args += ["-movflags", "+faststart"]
    _run_ffmpeg([*args, dst], timeout=timeout)


def _transcode_segment(
    src: str, dst: str, start_sec: float, duration_sec: float,
    timeout: Optional[float] = None,
) -> None:
    """视频转码 ProRes 422 + 音频流复制，帧精确。"""
    args = [
        "-ss", f"{start_sec:.6f}",
        "-i", src,
        "-t", f"{duration_sec:.6f}",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "prores_ks",
        "-profile:v", "3",
        "-pix_fmt", "yuv422p10le",
        "-c:a", "copy",
        dst,
    ]
    _run_ffmpeg(args, timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

def consolidate_project(
    project: EditProject,
    *,
    sequences: Iterable[Any],
    target_dir: str,
    mode: str = MODE_REMUX,
    handles_frames: int = 12,
    media_dir: str = "media",
    ffmpeg_timeout: Optional[float] = None,
    progress_cb: Optional[Callable[[ConsolidateProgress], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[EditProject, List[Any], ConsolidateReport]:
    """按时间线使用范围裁剪收集工程媒体。

    Args:
        project: 当前工程
        sequences: 全部时间线序列（决定裁剪范围与 clip 重映射）
        target_dir: 归档根目录（自动创建；媒体落在其 media/ 子目录）
        mode: ``remux``（流复制，GOP 对齐）或 ``transcode``（ProRes，帧精确）
        handles_frames: 每段前后手柄帧
        media_dir: 归档内媒体子目录名
        ffmpeg_timeout: 单段 ffmpeg 超时秒数（None 取默认
            ``_DEFAULT_FFMPEG_TIMEOUT_S``，不再无限等待——AUD-16311）
        progress_cb: AUD-16311 进度回调——每个分段开始时收到
            ``ConsolidateProgress``（phase="segment"），全部完成后收到
            一次 phase="done"。回调在调用线程同步执行，应快速返回。
        cancel_check: AUD-16311 取消检查点——分段间调用，返回 True 时
            抛出 :class:`ConsolidateCancelled`（分段边界取消，不再继续
            后续素材/分段）。

    Returns:
        (新工程, 重映射后的序列列表, 报告)。新工程中原始素材已被分段素材
        替换（素材表 + 素材箱同步），序列 clip 指向分段并平移 source_start。

    Raises:
        ConsolidateCancelled: ``cancel_check`` 在分段边界返回 True。
    """
    if mode not in CONSOLIDATE_MODES:
        raise ValueError(f"Unknown consolidate mode: {mode!r}")
    sequences = list(sequences)
    report = ConsolidateReport(
        archive_root=target_dir, mode=mode, handles_frames=handles_frames
    )
    # AUD-16311：默认有界超时，杜绝 UI 线程被无超时 ffmpeg 永久挂死。
    segment_timeout = (
        ffmpeg_timeout if ffmpeg_timeout is not None
        else _DEFAULT_FFMPEG_TIMEOUT_S
    )

    ranges_by_asset = compute_used_ranges(sequences, handles_frames=handles_frames)
    usage_index = (
        build_asset_usage_index(sequences, project) if sequences else None
    )
    used_ids = frozenset(ranges_by_asset)

    media_root = os.path.join(target_dir, media_dir)
    os.makedirs(media_root, exist_ok=True)

    # ── AUD-16311：进度/取消支持 ──
    # 分段总数预统计（与主循环的 still/缺失/虚拟判定同一口径），作为
    # progress_cb 的分母；完成数在主循环中随 record 追加递增。
    total_segments = 0
    for asset_id in sorted(used_ids):
        asset = project.assets.get(asset_id)
        if asset is None or asset.is_virtual:
            continue
        src = asset.path
        if not os.path.exists(src):
            continue
        if asset.media_type in ("image", "image_sequence") or os.path.isdir(src):
            total_segments += 1
        else:
            total_segments += len(ranges_by_asset[asset_id])
    completed_segments = 0

    def _cancel_requested() -> bool:
        return cancel_check is not None and bool(cancel_check())

    def _progress(
        phase: str,
        asset_id: str,
        display_name: str,
        *,
        segment_index: int,
        segment_count: int,
    ) -> None:
        if progress_cb is None:
            return
        progress_cb(ConsolidateProgress(
            phase=phase,
            asset_id=asset_id,
            display_name=display_name,
            segment_index=segment_index,
            segment_count=segment_count,
            completed_segments=completed_segments,
            total_segments=total_segments,
        ))

    def _segment_boundary(asset_id: str, display_name: str,
                          segment_index: int, segment_count: int) -> None:
        """AUD-16311：分段边界检查点——先看取消，再报进度。"""
        if _cancel_requested():
            raise ConsolidateCancelled(
                f"Consolidate cancelled after {completed_segments} segment(s)"
            )
        _progress(
            "segment", asset_id, display_name,
            segment_index=segment_index, segment_count=segment_count,
        )

    # AUD-16309：输出文件名去重登记表——同名素材（多卡 DSC_0001 等）的
    # 分段相互覆盖会让归档工程指向错误媒体内容，所有 out_name 生成后
    # 必须经 _unique_out_name 登记。
    used_names: Set[str] = set()
    records: List[ConsolidateSegmentRecord] = []
    missing_usage: Dict[str, Tuple[str, ...]] = {}
    warnings: List[str] = []
    # asset_id → [(new_asset_id, aligned_start_frame), ...] 按区间升序
    seg_assign: Dict[str, List[Tuple[str, int, int, int]]] = {}
    #            asset_id → [(new_asset_id, aligned_start, range_start, range_end)]
    pending_assets: List[Tuple[MediaAsset, List[Any]]] = []  # (分段素材, plans)

    for asset_id in sorted(used_ids):
        asset = project.assets.get(asset_id)
        if asset is None:
            warnings.append(f"Clip references unknown asset {asset_id}; kept as-is")
            continue
        if asset.is_virtual:
            report.skipped_virtual += (asset_id,)  # type: ignore[assignment]
            continue

        ranges = ranges_by_asset[asset_id]
        src = asset.path
        if not os.path.exists(src):
            locations = (
                usage_index.locations_for(asset_id) if usage_index else ()
            )
            missing_usage[asset_id] = tuple(loc.format() for loc in locations)
            records.append(ConsolidateSegmentRecord(
                asset_id=asset_id,
                display_name=asset.display_name,
                source_path=src,
                segment_index=1,
                mode=mode,
                requested_start_frame=ranges[0].start,
                requested_end_frame=ranges[0].end,
                aligned_start_frame=0,
                status=STATUS_MISSING,
            ))
            continue

        try:
            file_size = os.path.getsize(src) if os.path.isfile(src) else 0
        except OSError:
            file_size = 0
        fps = _asset_fps(project, asset, sequences)

        base = os.path.splitext(
            os.path.basename(asset.display_name or src)
        )[0] or f"asset_{asset_id[:8]}"
        base = _sanitize_name(base, fallback=f"asset_{asset_id[:8]}")

        # 逐段执行：关键帧扫描（规划）+ ffmpeg 复制在同一 try 内，
        # 损坏媒体在任一步失败都只记录该段失败，不中断整批。
        seg_entries: List[Tuple[str, int, int, int]] = []
        seg_assets: List[MediaAsset] = []
        seg_ok = True

        # 静态素材（图片/图片序列/目录型源）无时间轴可裁，整份复制
        # （与 collect 一致），偏移恒 0；多个使用区间统一并入唯一分段。
        is_still = (
            asset.media_type in ("image", "image_sequence")
            or os.path.isdir(src)
        )
        if is_still:
            still_ext = os.path.splitext(src)[1] if os.path.isfile(src) else ""
            plan = _SegmentPlan(
                asset_id=asset_id, index=1,
                start_frame=0, end_frame=ranges[-1].end,
                start_sec=0.0, duration_sec=0.0,
                out_name=_unique_out_name(
                    f"{base}_c1{still_ext}", media_root, used_names
                ),
            )
            plan_iter = [(plan, 0, 0, ranges[-1].end)]

        if not is_still:
            for idx, rng in enumerate(ranges, start=1):
                _segment_boundary(
                    asset_id, asset.display_name,
                    segment_index=idx, segment_count=len(ranges),
                )
                aligned_frame = rng.start
                status = STATUS_CONSOLIDATED
                new_asset: Optional[MediaAsset] = None
                sha, size = "", 0
                out_name = f"{base}_c{idx}.err"
                dst = os.path.join(media_root, out_name)
                try:
                    start_sec = rng.start / fps
                    end_sec = rng.end / fps
                    aligned_sec = start_sec
                    if mode == MODE_REMUX and asset.media_type != "audio":
                        kf = _last_keyframe_at_or_before(src, start_sec)
                        if kf is not None:
                            aligned_sec = min(kf, start_sec)
                    aligned_frame = int(round(aligned_sec * fps))
                    aligned_frame = min(aligned_frame, rng.start)  # 浮点防御
                    ext = (
                        os.path.splitext(src)[1]
                        if mode == MODE_REMUX
                        else _TRANSCODE_EXT
                    )
                    # AUD-16309：登记去重后再落盘，绝不覆盖同名分段
                    out_name = _unique_out_name(
                        f"{base}_c{idx}{ext}", media_root, used_names
                    )
                    dst = os.path.join(media_root, out_name)
                    plan = _SegmentPlan(
                        asset_id=asset_id, index=idx,
                        start_frame=aligned_frame, end_frame=rng.end,
                        start_sec=aligned_sec,
                        duration_sec=end_sec - aligned_sec,
                        out_name=out_name,
                    )
                    if mode == MODE_TRANSCODE and asset.media_type != "audio":
                        _transcode_segment(
                            src, dst, plan.start_sec, plan.duration_sec,
                            timeout=segment_timeout,
                        )
                    else:
                        _remux_segment(
                            src, dst, plan.start_sec, plan.duration_sec,
                            timeout=segment_timeout,
                        )
                    sha = _sha256_of(dst)
                    size = os.path.getsize(dst)
                    new_asset = _segment_asset(asset, dst, plan, fps)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Consolidate segment failed %s: %s", src, exc)
                    status = STATUS_FAILED
                    sha, size = "", 0
                    new_asset = None
                    warnings.append(f"{out_name}: {exc}")
                    seg_ok = False

                records.append(ConsolidateSegmentRecord(
                    asset_id=asset_id,
                    display_name=asset.display_name,
                    source_path=src,
                    segment_index=idx,
                    mode=mode,
                    requested_start_frame=rng.start,
                    requested_end_frame=rng.end,
                    aligned_start_frame=aligned_frame,
                    new_asset_id=new_asset.asset_id if new_asset else "",
                    archived_path=(
                        os.path.join(media_dir, out_name)
                        if status != STATUS_FAILED
                        else ""
                    ),
                    status=status,
                    sha256=sha,
                    file_size=size,
                    source_file_size=file_size,
                ))
                completed_segments += 1
                if new_asset is not None:
                    seg_entries.append(
                        (new_asset.asset_id, aligned_frame, rng.start, rng.end)
                    )
                    seg_assets.append(new_asset)
        else:
            for plan, aligned_frame, req_start, req_end in plan_iter:
                _segment_boundary(
                    asset_id, asset.display_name,
                    segment_index=plan.index, segment_count=1,
                )
                dst = os.path.join(media_root, plan.out_name)
                status = STATUS_COPIED_IMAGE
                new_asset = None
                sha, size = "", 0
                try:
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                    sha = _sha256_of(dst) if os.path.isfile(dst) else ""
                    size = (
                        os.path.getsize(dst) if os.path.isfile(dst)
                        else _dir_size(dst)
                    )
                    new_asset = _segment_asset(asset, dst, plan, fps)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Consolidate copy failed %s: %s", src, exc)
                    status = STATUS_FAILED
                    warnings.append(f"{plan.out_name}: {exc}")
                    seg_ok = False
                records.append(ConsolidateSegmentRecord(
                    asset_id=asset_id,
                    display_name=asset.display_name,
                    source_path=src,
                    segment_index=plan.index,
                    mode=mode,
                    requested_start_frame=req_start,
                    requested_end_frame=req_end,
                    aligned_start_frame=aligned_frame,
                    new_asset_id=new_asset.asset_id if new_asset else "",
                    archived_path=(
                        os.path.join(media_dir, plan.out_name)
                        if status != STATUS_FAILED
                        else ""
                    ),
                    status=status,
                    sha256=sha,
                    file_size=size,
                    source_file_size=file_size,
                ))
                completed_segments += 1
                if new_asset is not None:
                    seg_entries.append(
                        (new_asset.asset_id, aligned_frame, req_start, req_end)
                    )
                    seg_assets.append(new_asset)

        if seg_ok and seg_assets:
            seg_assign[asset_id] = seg_entries
            pending_assets.append((asset, seg_assets))

    # ── 工程重建：移除已分段素材，直接插入分段素材（asset_id 已在
    #    _segment_asset 生成，import_asset 的去重/新 ID 逻辑不适用），
    #    素材箱引用同步替换 ──
    new_project = project
    if seg_assign:
        consolidated_ids = set(seg_assign)
        new_assets = {
            aid: a for aid, a in project.assets.items()
            if aid not in consolidated_ids
        }
        for _orig, seg_assets in pending_assets:
            for sa in seg_assets:
                new_assets[sa.asset_id] = sa
        new_bins = {
            bid: _rebuild_bin_assets(bin_, seg_assign)
            for bid, bin_ in project.bins.items()
        }
        new_project = dataclasses.replace(
            project, assets=new_assets, bins=new_bins
        )

    # ── 序列重映射 ──
    new_sequences, remapped_clips, unmapped = _remap_sequences(
        sequences, seg_assign
    )
    report.records = tuple(records)
    report.missing_usage = missing_usage
    report.remapped_clips = remapped_clips
    report.unmapped_clip_ids = tuple(unmapped)
    report.warnings = tuple(warnings)
    report.skipped_unused = tuple(
        aid for aid, a in project.assets.items()
        if aid not in used_ids and not a.is_virtual
    )
    logger.info(
        "Consolidate (%s) -> %s: %d segments (%d failed), %d remapped clips, "
        "%d unmapped",
        mode, target_dir, len(report.consolidated), len(report.failures),
        len(remapped_clips), len(unmapped),
    )
    # AUD-16311：全部完成的最终进度（completed == total）。
    _progress(
        "done", "", "",
        segment_index=completed_segments, segment_count=total_segments,
    )
    return new_project, new_sequences, report


def _dir_size(path: str) -> int:
    """目录总字节数（图片序列等目录型源）。"""
    total = 0
    for _root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(_root, f))
            except OSError:
                pass
    return total


def _sanitize_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", name).strip("._")
    cleaned = cleaned[:100] or fallback
    # W10（与 project_archive._sanitize_name 同契约）：Windows 保留设备
    # 名带扩展名同样非法（CON.mov → 设备），按词干匹配前缀化；尾部
    # 点/空格被 NTFS 静默剥离（创建名≠请求名，输出路径错位）。
    stem = cleaned.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} | {
            f"COM{i}" for i in range(1, 10)} | {
            f"LPT{i}" for i in range(1, 10)}:
        cleaned = f"_{cleaned}"
    return cleaned.rstrip(". ")


def _unique_out_name(candidate: str, media_root: str, used_names: Set[str]) -> str:
    """AUD-16309：分段输出名去重，返回不冲突的文件名并登记。

    输出名冲突有两种来源，都会让 ffmpeg ``-y``/shutil 静默覆盖已有媒体
    内容（归档工程指向错误素材画面、sha256 与最终文件不匹配）：

    - 同一批整理中两个素材同名（多卡拍摄的 DSC_0001.MP4 等）；
    - 归档目录里已有同名文件（对同一 target_dir 重复整理）。

    冲突时在扩展名前追加递增序号（``a_c1.mp4`` → ``a_c1_2.mp4``）。
    """
    stem, ext = os.path.splitext(candidate)
    name = candidate
    n = 2
    while name in used_names or os.path.exists(os.path.join(media_root, name)):
        name = f"{stem}_{n}{ext}"
        n += 1
    used_names.add(name)
    return name


def _segment_asset(
    asset: MediaAsset, seg_path: str, plan: _SegmentPlan, fps: float
) -> MediaAsset:
    """从原素材派生分段素材（全新 asset_id，重置代理，更新时长元数据）。"""
    metadata = dict(asset.metadata)
    frames = max(0, plan.end_frame - plan.start_frame)
    if fps > 0:
        metadata["total_frames"] = frames
        metadata["duration"] = frames / fps
        metadata["fps"] = fps
    stem, _ext = os.path.splitext(os.path.basename(asset.display_name or seg_path))
    return dataclasses.replace(
        asset,
        asset_id=str(uuid.uuid4()),
        path=seg_path,
        display_name=f"{stem}_c{plan.index}",
        proxy_path="",
        proxy_status="none",
        is_offline=False,
        metadata=metadata,
    )


def _rebuild_bin_assets(bin_: Any, seg_assign: Mapping[str, Sequence[Any]]):
    """素材箱内旧素材引用替换为对应分段引用（保持原顺序位）。"""
    old_ids = getattr(bin_, "asset_ids", ())
    new_ids: List[str] = []
    for aid in old_ids:
        entries = seg_assign.get(aid)
        if entries:
            new_ids.extend(e[0] for e in entries)
        else:
            new_ids.append(aid)
    if tuple(new_ids) == tuple(old_ids):
        return bin_
    return dataclasses.replace(bin_, asset_ids=tuple(new_ids))


def _remap_sequences(
    sequences: Sequence[Any],
    seg_assign: Mapping[str, Sequence[Tuple[str, int, int, int]]],
) -> Tuple[List[Any], Dict[str, str], List[str]]:
    """把引用已分段素材的 clip 重映射到分段素材并平移 source_start。

    Returns:
        (新序列列表, clip_id → "分段名 @ 新source_start", 未映射 clip_id)
    """
    # clip 定位：asset_id → [(new_id, aligned, start, end)] 升序
    remapped: Dict[str, str] = {}
    unmapped: List[str] = []
    new_sequences: List[Any] = []

    def _segment_for(asset_id: str, source_start: int):
        entries = seg_assign.get(asset_id)
        if not entries:
            return None
        for new_id, aligned, start, end in entries:
            if start <= source_start < end:
                return new_id, aligned
        # 防御：起点不在任何区间（理论上合并已保证），取首个不早于它的段
        for new_id, aligned, start, end in entries:
            if source_start < end:
                return new_id, aligned
        return None

    for seq in sequences:
        def fix_tracks(tracks: Sequence[Any]) -> Tuple[tuple, bool]:
            new_tracks: List[Any] = []
            changed = False
            for track in tracks:
                new_clips: List[Any] = []
                track_changed = False
                for clip in getattr(track, "clips", ()):
                    asset_id = getattr(clip, "asset_id", "")
                    if asset_id not in seg_assign:
                        new_clips.append(clip)
                        continue
                    seg = _segment_for(asset_id, int(clip.source_start))
                    if seg is None:
                        unmapped.append(clip.clip_id)
                        new_clips.append(clip)
                        track_changed = True
                        continue
                    new_id, aligned = seg
                    new_start = max(0, int(clip.source_start) - aligned)
                    new_clips.append(dataclasses.replace(
                        clip, asset_id=new_id, source_start=new_start
                    ))
                    remapped[clip.clip_id] = f"{new_id[:8]} @ {new_start}"
                    track_changed = True
                if track_changed:
                    new_tracks.append(dataclasses.replace(track, clips=tuple(new_clips)))
                    changed = True
                else:
                    new_tracks.append(track)
            return tuple(new_tracks), changed

        video_tracks, v_changed = fix_tracks(getattr(seq, "video_tracks", ()))
        audio_tracks, a_changed = fix_tracks(getattr(seq, "audio_tracks", ()))
        if v_changed or a_changed:
            new_sequences.append(seq._rebuild(
                video_tracks=video_tracks, audio_tracks=audio_tracks
            ))
        else:
            new_sequences.append(seq)

    return new_sequences, remapped, unmapped
