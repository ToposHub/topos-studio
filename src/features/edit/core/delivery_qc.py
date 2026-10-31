"""Delivery QC —— 项目级交付质量门（P1-19）。

背景（计划表 P1-19）：导出前已有响度/字体/效果/路由的**局部** preflight
（timeline_export.py），但缺少统一的项目级交付检查：黑帧/离线素材、
音画时长、字幕越界、输出文件完整性等没有统一的 blocking/warning/degraded
分级和可归档报告。

本模块提供导出前（模型级）与导出后（文件级）两段检查：

- **模型级**（导出前，纯数据、可无头测试）：
  离线素材（BLOCKING）、音画时长差（WARNING）、字幕越界（WARNING）、
  空时间线（WARNING，空画布导出是合法路径）、素材/时间线帧率不一致
  （WARNING，P1-15 混合帧率契约）；
- **文件级**（导出后）：输出存在/非空（BLOCKING）、大小与 SHA-256
  （INFO，随报告归档，供 P1-18 manifest 引用）。

分级语义：
- BLOCKING —— 不应交付，导出流程应中止（``report.passed`` 为 False）；
- WARNING  —— 可以导出，但报告必须保留该发现；
- INFO     —— 事实记录（哈希、统计），不是问题。

设计约束：
- 检查必须保守：不能把合法导出路径（空时间线、纯音频轨等）误判为阻断；
- 全部纯模型 + 标准库文件操作，无 Qt / GPU / 解码器依赖；
- 与 timeline_export 的 ExportPreflightResult 互补而非替代：
  preflight 负责"这次渲染能不能跑"，QC 负责"交付物可不可信"。
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DELIVERY_QC_SCHEMA = "topos.delivery_qc.v1"

# 音画时长差超过该帧数 → WARNING（1 帧内的差通常是尾块对齐，不告警）
DEFAULT_AV_TOLERANCE_FRAMES = 2


class QCSeverity(str, Enum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class QCFinding:
    """单条 QC 发现。check_id 稳定，供报告/过滤/回归测试引用。"""

    check_id: str
    severity: QCSeverity
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "severity": self.severity.value,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class DeliveryQCReport:
    """一次交付 QC 的完整报告。"""

    findings: Tuple[QCFinding, ...] = ()
    checked_at: str = ""

    @property
    def blocking(self) -> Tuple[QCFinding, ...]:
        return tuple(f for f in self.findings if f.severity is QCSeverity.BLOCKING)

    @property
    def warnings(self) -> Tuple[QCFinding, ...]:
        return tuple(f for f in self.findings if f.severity is QCSeverity.WARNING)

    @property
    def infos(self) -> Tuple[QCFinding, ...]:
        return tuple(f for f in self.findings if f.severity is QCSeverity.INFO)

    @property
    def passed(self) -> bool:
        """无 BLOCKING 即视为通过（WARNING 不阻断，但保留在报告中）。"""
        return not self.blocking

    @property
    def blocking_messages(self) -> List[str]:
        return [f.message for f in self.blocking]

    @property
    def warning_messages(self) -> List[str]:
        return [f.message for f in self.warnings]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": DELIVERY_QC_SCHEMA,
            "checked_at": self.checked_at,
            "passed": self.passed,
            "counts": {
                "blocking": len(self.blocking),
                "warning": len(self.warnings),
                "info": len(self.infos),
            },
            "findings": [f.to_dict() for f in self.findings],
        }


def _iter_tracks(sequence: Any, track_type: str):
    for track in getattr(sequence, "all_tracks", ()):
        if getattr(track, "track_type", "") == track_type:
            yield track


def _check_offline_media(sequence: Any, project: Optional[Any]) -> List[QCFinding]:
    """离线/缺失素材 → BLOCKING（渲染输出将出现黑帧或占位画面）。

    project 为 None 或鸭子类型/mock（字段类型不可判定）时跳过：
    无法解析引用时逐条报阻断只会产生噪声，甚至误伤调用方。
    """
    if project is None:
        return []
    lookup = getattr(project, "get_asset_by_id", None)
    if not callable(lookup):
        return []
    findings: List[QCFinding] = []
    seen: set = set()
    for track in _iter_tracks(sequence, "video"):
        for clip in track.clips:
            asset_id = getattr(clip, "asset_id", "")
            if not asset_id or asset_id in seen:
                continue
            seen.add(asset_id)
            asset = lookup(asset_id)
            if asset is not None and not isinstance(
                getattr(asset, "is_offline", None), bool
            ):
                # mock/鸭子类型素材：无法判定离线状态，跳过
                continue
            # 宿主侧虚拟素材（嵌套序列/comp_clip/adjustment 等）本就没有
            # 真实源文件，画面由宿主递归渲染——不按"源文件不存在"阻断。
            if asset is not None and bool(getattr(asset, "is_virtual", False)):
                continue
            raw_path = getattr(asset, "path", "") if asset is not None else ""
            path = raw_path if isinstance(raw_path, str) else ""
            offline = bool(getattr(asset, "is_offline", False)) if asset is not None else True
            missing = bool(path) and not os.path.exists(path)
            if offline or missing or asset is None:
                name = getattr(asset, "display_name", "") if asset is not None else asset_id
                if not isinstance(name, str):
                    name = asset_id
                reason = "素材标记为离线" if offline else (
                    "源文件不存在" if missing else "素材未在工程中找到"
                )
                findings.append(QCFinding(
                    check_id="media.offline",
                    severity=QCSeverity.BLOCKING,
                    message=f"视频轨引用了不可用的素材：{name or asset_id}（{reason}）",
                    details={
                        "asset_id": asset_id,
                        "path": path,
                        "reason": reason,
                        "clip_id": getattr(clip, "clip_id", ""),
                        "track_id": getattr(track, "track_id", ""),
                    },
                ))
    return findings


def _content_end(sequence: Any, track_type: str) -> int:
    """该类型轨道上**有剪辑内容**的最长结束帧（空轨道不参与）。"""
    return max(
        (
            getattr(track, "duration_frames", 0)
            for track in _iter_tracks(sequence, track_type)
            if track.clips or track.subtitle_clips
        ),
        default=0,
    )


def _check_av_duration(
    sequence: Any, tolerance_frames: int
) -> List[QCFinding]:
    """音画时长差 → WARNING（超过容差的时长差通常意味着漏轨或多余尾音）。

    只比较有内容的轨道：留空的备用轨不应触发误报。
    """
    video_end = _content_end(sequence, "video")
    audio_end = _content_end(sequence, "audio")
    findings: List[QCFinding] = []
    if video_end > 0 and audio_end > 0 and abs(video_end - audio_end) > tolerance_frames:
        longer, shorter = ("视频", "音频") if video_end > audio_end else ("音频", "视频")
        findings.append(QCFinding(
            check_id="av.duration_mismatch",
            severity=QCSeverity.WARNING,
            message=(
                f"{longer}内容比{shorter}内容长 "
                f"{abs(video_end - audio_end)} 帧"
                f"（视频轨结束 {video_end}，音频轨结束 {audio_end}）"
            ),
            details={
                "video_end_frames": video_end,
                "audio_end_frames": audio_end,
                "tolerance_frames": tolerance_frames,
            },
        ))
    return findings


def _check_subtitle_bounds(sequence: Any) -> List[QCFinding]:
    """字幕超出音视频内容范围 → WARNING（导出会被截断，交付侧应知情）。

    参照物取视频/音频内容结束帧的最大值，而不是 sequence.duration_frames
    ——后者把字幕轨自身也计入，会造成循环参照（字幕永远不会越界）。
    """
    findings: List[QCFinding] = []
    total = max(_content_end(sequence, "video"), _content_end(sequence, "audio"))
    if total <= 0:
        return findings
    for track in _iter_tracks(sequence, "subtitle"):
        for sub in track.subtitle_clips:
            end = sub.timeline_start + max(0, sub.timeline_duration)
            if sub.timeline_start < 0 or end > total:
                findings.append(QCFinding(
                    check_id="subtitle.beyond_duration",
                    severity=QCSeverity.WARNING,
                    message=(
                        f"字幕「{sub.text[:20]}」超出时间线范围"
                        f"（{sub.timeline_start}–{end}，时间线共 {total} 帧），"
                        "导出时会被截断"
                    ),
                    details={
                        "clip_id": sub.clip_id,
                        "start": sub.timeline_start,
                        "end": end,
                        "timeline_duration": total,
                    },
                ))
    return findings


def _check_empty_timeline(sequence: Any) -> List[QCFinding]:
    """空时间线 → WARNING（空画布导出是合法路径，但交付侧应知情）。"""
    clip_count = sum(len(t.clips) for t in _iter_tracks(sequence, "video"))
    clip_count += sum(len(t.clips) for t in _iter_tracks(sequence, "audio"))
    sub_count = sum(len(t.subtitle_clips) for t in _iter_tracks(sequence, "subtitle"))
    if clip_count == 0 and sub_count == 0:
        return [QCFinding(
            check_id="timeline.empty",
            severity=QCSeverity.WARNING,
            message="时间线没有任何剪辑或字幕，将导出空画布",
            details={},
        )]
    return []


def _check_asset_fps_mismatch(sequence: Any, project: Optional[Any]) -> List[QCFinding]:
    """素材与时间线帧率不一致 → WARNING（混合帧率契约属 P1-15，先如实披露）。"""
    findings: List[QCFinding] = []
    fps_obj = getattr(sequence, "fps", None)
    timeline_fps = fps_obj.to_float() if fps_obj is not None else None
    if timeline_fps is None or project is None:
        return findings
    lookup = getattr(project, "get_asset_by_id", None)
    if not callable(lookup):
        return findings
    seen: set = set()
    for track_type in ("video", "audio"):
        for track in _iter_tracks(sequence, track_type):
            for clip in track.clips:
                asset = lookup(getattr(clip, "asset_id", ""))
                if asset is None:
                    continue
                meta_fps = getattr(asset, "metadata", {}).get("fps")
                if not isinstance(meta_fps, (int, float)) or asset.asset_id in seen:
                    continue
                seen.add(asset.asset_id)
                if abs(float(meta_fps) - float(timeline_fps)) > 0.5:
                    findings.append(QCFinding(
                        check_id="media.fps_mismatch",
                        severity=QCSeverity.WARNING,
                        message=(
                            f"素材「{asset.display_name}」帧率 {meta_fps} "
                            f"与时间线帧率 {timeline_fps} 不一致，"
                            "播放/导出按时间线帧率重采样"
                        ),
                        details={
                            "asset_id": asset.asset_id,
                            "asset_fps": float(meta_fps),
                            "timeline_fps": float(timeline_fps),
                        },
                    ))
    return findings


def _sha256_of(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def inspect_output_file(output_path: str) -> List[QCFinding]:
    """导出后文件完整性检查（BLOCKING）+ 哈希记录（INFO）。

    独立于 ``run_delivery_qc`` 单独调用：导出完成后由 exporter 调用，
    结果并入 manifest / QC 归档。
    """
    findings: List[QCFinding] = []
    if not output_path:
        return findings
    if not os.path.exists(output_path):
        findings.append(QCFinding(
            check_id="output.missing",
            severity=QCSeverity.BLOCKING,
            message=f"导出输出文件不存在：{output_path}",
            details={"path": output_path},
        ))
        return findings
    size = os.path.getsize(output_path)
    if size <= 0:
        findings.append(QCFinding(
            check_id="output.empty",
            severity=QCSeverity.BLOCKING,
            message=f"导出输出文件为空（0 字节）：{output_path}",
            details={"path": output_path, "size": size},
        ))
        return findings
    try:
        digest = _sha256_of(output_path)
    except OSError as exc:
        findings.append(QCFinding(
            check_id="output.hash_failed",
            severity=QCSeverity.WARNING,
            message=f"输出文件哈希计算失败：{exc}",
            details={"path": output_path},
        ))
    else:
        findings.append(QCFinding(
            check_id="output.integrity",
            severity=QCSeverity.INFO,
            message=f"输出文件就绪（{size} 字节，SHA-256 {digest[:16]}…）",
            details={"path": output_path, "size": size, "sha256": digest},
        ))
    return findings


def run_delivery_qc(
    sequence: Any,
    project: Optional[Any] = None,
    *,
    av_tolerance_frames: int = DEFAULT_AV_TOLERANCE_FRAMES,
    output_path: Optional[str] = None,
) -> DeliveryQCReport:
    """执行交付 QC 并生成报告。

    Args:
        sequence: TimelineSequence（纯模型，无需 UI）。
        project: EditProject（素材查找用；None 时跳过素材相关检查）。
        av_tolerance_frames: 音画时长差容差（帧）。
        output_path: 若提供，同时执行导出后文件级检查（用于重开校验）。

    Returns:
        DeliveryQCReport —— ``passed`` 为 False 表示存在 BLOCKING，
        导出流程应中止并展示 ``blocking_messages``。
    """
    from datetime import datetime, timezone

    findings: List[QCFinding] = []
    findings.extend(_check_offline_media(sequence, project))
    findings.extend(_check_av_duration(sequence, av_tolerance_frames))
    findings.extend(_check_subtitle_bounds(sequence))
    findings.extend(_check_empty_timeline(sequence))
    findings.extend(_check_asset_fps_mismatch(sequence, project))
    if output_path:
        findings.extend(inspect_output_file(output_path))

    return DeliveryQCReport(
        findings=tuple(findings),
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
