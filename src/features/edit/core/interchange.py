# F1.3/R3（2026-10-23 修订）：本模块非休眠——时间码函数有 6 处生产调用
# （edit_page/source_viewer/timeline_view/caption_workspace/recording_manager/
# scene_cut），BWF bext 由 recording_manager 生产写入；FCPXML/OTIO 经
# file_controller 接 File 菜单。仍无生产入口的部分：CSV cue sheet
# （见能力注册表 DORMANT 声明）。
"""
Audio Interchange — 互操作导出（AM-08 阶段 4.5）。

专业影视后期与其他 NLE/DAW 的音频交换格式。根据计划表 4.5 优先级：

1. **BWF timecode**：Broadcast Wave + 时间码元数据（已在 recording_manager.py 的
   write_take_wav 中实现 ICMT/IART/INAM tag）。本模块提供时间码计算与 cue sheet。
2. **CSV cue sheet**：纯文本 cue/EDL，列出所有 clip 的入/出点、时长、轨道、名称，
   供外部 DAW 对齐（不依赖二进制格式，最通用）。
3. **Stems**：已在 stem_export.py 实现。
4. **AAF/OMF/XML**：评估库与许可（见模块底部 INTERCHANGE_CAPABILITIES）。

设计原则（计划表 4.5）：
- 使用外部 NLE/DAW 导入样例，timecode、长度、轨道名、stem channel layout 被正确识别。
- 不引入有许可风险的二进制库（AAF/OMF 评估后决定）。
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import TimelineSequence

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 时间码（SMPTE ST 12-1）转换
# ─────────────────────────────────────────────────────────────────────────────

def _df_profile(fps: float) -> Optional[Tuple[int, int]]:
    """识别 NTSC drop-frame 帧率，返回 (名义整数帧率, 每分钟丢弃帧数)。

    30000/1001 → (30, 2)；60000/1001 → (60, 4)。其余帧率不属于 DF 家族。
    """
    f = float(fps)
    if abs(f - 30000.0 / 1001.0) < 0.02:
        return 30, 2
    if abs(f - 60000.0 / 1001.0) < 0.05:
        return 60, 4
    return None


def _fps_float(fps: Any) -> float:
    """帧率统一转 float（Rational 只有 __float__，无比较运算）。"""
    return float(fps)


def frames_to_timecode(
    frame: int,
    fps: float,
    drop_frame: bool = False,
) -> str:
    """帧号 → SMPTE 时间码（HH:MM:SS:FF，drop-frame 用 HH:MM:SS;FF）。

    P0-05：drop-frame 使用 SMPTE ST 12-1 算法——每个 10 分钟块含
    9 个"短分钟"（少 drop 个帧号），第 0/10/20… 分钟完整。帧号是真实
    累计帧数；显示值跳过每分钟开头的 drop 个编号。旧实现把所有分钟
    都按短分钟计算，从第 2 分钟起即产生不可显示的错误时间码。

    Args:
        frame: 帧号（0-based，真实累计帧数）
        fps: 帧率（24/25/29.97/30/59.94/60，可为 Rational）
        drop_frame: 是否 drop-frame（仅 29.97/59.94 家族合法）

    Returns:
        "HH:MM:SS:FF"（NDF）或 "HH:MM:SS;FF"（DF）字符串；
        fps<=0 时返回 "00:00:00:00"（保持历史行为）

    Raises:
        ValueError: drop_frame=True 但 fps 不是 NTSC DF 帧率
    """
    if _fps_float(fps) <= 0:
        return "00:00:00:00"
    # 审计修复：负帧不属于 SMPTE 表达域——旧行为经 Python 负数 divmod
    # 产出 "-1:59:59;29" 这类 parse_timecode 无法读回的断裂串。
    # 统一钳制到 0（生产调用方各自 max(0,…) 已是这个语义）。
    frame = max(0, int(frame))

    if drop_frame:
        profile = _df_profile(fps)
        if profile is None:
            raise ValueError(
                f"drop_frame=True requires 30000/1001 or 60000/1001 fps, got {fps}"
            )
        nominal, drop = profile
        frames_per_10min = nominal * 600 - 9 * drop
        first_minute = nominal * 60  # 每 10 分钟块的第 0 分钟不丢帧号
        per_minute = first_minute - drop

        block, f = divmod(frame, frames_per_10min)
        if f < first_minute:
            display_local = f
        else:
            minute = 1 + (f - first_minute) // per_minute  # 1..9
            display_local = (
                first_minute * minute + drop + (f - first_minute) % per_minute
            )
        display = block * nominal * 600 + display_local

        ff = display % nominal
        ss = (display // nominal) % 60
        mm = (display // (nominal * 60)) % 60
        hh = display // (nominal * 3600)
        return f"{hh:02d}:{mm:02d}:{ss:02d};{ff:02d}"

    fps_int = round(_fps_float(fps))
    if fps_int <= 0:
        return "00:00:00:00"
    ff = frame % fps_int
    total_seconds = frame // fps_int
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def parse_timecode(tc: str) -> Tuple[int, int, int, int]:
    """解析 SMPTE 时间码字符串为 (hh, mm, ss, ff)。

    接受 ':' 或 ';'（DF）分隔。空串返回全零（保持历史行为）。

    Raises:
        ValueError: 字段数不是 4、字段非整数或为负
    """
    if not tc:
        return 0, 0, 0, 0
    parts = tc.replace(";", ":").replace("-", ":").split(":")
    if len(parts) != 4:
        raise ValueError(f"Invalid timecode (expected HH:MM:SS:FF): {tc!r}")
    try:
        values = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"Invalid timecode (non-numeric field): {tc!r}") from None
    if any(v < 0 for v in values):
        raise ValueError(f"Invalid timecode (negative field): {tc!r}")
    return values[0], values[1], values[2], values[3]


def timecode_to_frames(tc: str, fps: float, drop_frame: bool = False) -> int:
    """SMPTE 时间码 → 帧号（frames_to_timecode 的精确逆运算）。

    P0-05：drop-frame 反向转换现在真正扣除被丢弃的帧号，与正向函数
    构成双射。非法 DF 帧（非整 10 分钟处 ff < drop）显式报错，而不是
    静默给出帧级偏移。

    Raises:
        ValueError: 格式非法、字段越界、DF 非法帧号，或
            drop_frame=True 但 fps 不是 NTSC DF 帧率
    """
    hh, mm, ss, ff = parse_timecode(tc)

    if drop_frame:
        profile = _df_profile(fps)
        if profile is None:
            raise ValueError(
                f"drop_frame=True requires 30000/1001 or 60000/1001 fps, got {fps}"
            )
        nominal, drop = profile
        if ss >= 60 or mm >= 60 or ff >= nominal:
            raise ValueError(f"Timecode field out of range: {tc!r}")
        if mm % 10 != 0 and ss == 0 and ff < drop:
            raise ValueError(
                f"Non-displayable drop-frame timecode "
                f"(ff<{drop} at minute {mm} second 0): {tc!r}"
            )

        display = (hh * 3600 + mm * 60 + ss) * nominal + ff
        frames_per_10min = nominal * 600 - 9 * drop
        first_minute = nominal * 60
        per_minute = first_minute - drop

        block, d_local = divmod(display, nominal * 600)
        if d_local < first_minute:
            f_local = d_local
        else:
            minute = d_local // first_minute  # 1..9
            f_local = (
                first_minute
                + (minute - 1) * per_minute
                + (d_local - first_minute * minute - drop)
            )
        return block * frames_per_10min + f_local

    fps_int = round(_fps_float(fps))
    if fps_int <= 0:
        raise ValueError(f"Invalid fps: {fps}")
    if ss >= 60 or mm >= 60 or ff >= fps_int:
        raise ValueError(f"Timecode field out of range: {tc!r}")
    return hh * 3600 * fps_int + mm * 60 * fps_int + ss * fps_int + ff


def frames_to_samples(frame: int, fps: float, sample_rate: int) -> int:
    """帧号 → 采样号（音视频同步）。

    fps 为 Rational 时走精确有理数运算，避免长时间线上的浮点漂移；
    float 帧率保持 int(frame / fps * sample_rate) 的历史语义。
    """
    if _fps_float(fps) <= 0 or sample_rate <= 0:
        return 0
    if hasattr(fps, "num") and hasattr(fps, "den"):
        frac = Fraction(int(fps.num), int(fps.den))
        return int(Fraction(int(frame) * int(sample_rate)) / frac)
    return int(frame / fps * sample_rate)


def samples_to_frames(samples: int, fps: float, sample_rate: int) -> int:
    """采样号 → 帧号（frames_to_samples 的逆，向零取整）。"""
    if _fps_float(fps) <= 0 or sample_rate <= 0:
        return 0
    if hasattr(fps, "num") and hasattr(fps, "den"):
        frac = Fraction(int(fps.num), int(fps.den))
        return int(Fraction(int(samples)) * frac / int(sample_rate))
    return int(samples / sample_rate * fps)


# ─────────────────────────────────────────────────────────────────────────────
# CSV Cue Sheet 导出
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CueEntry:
    """单个 cue（clip）的元数据。"""
    track_name: str
    clip_name: str
    timeline_start_tc: str  # HH:MM:SS:FF
    timeline_end_tc: str
    duration_tc: str
    duration_seconds: float
    asset_name: str
    sample_rate: int = 48000
    channels: int = 2


@dataclass
class CueSheet:
    """cue sheet（所有 clip 的时间码清单）。"""
    project_name: str = ""
    fps: float = 24.0
    drop_frame: bool = False
    total_duration_tc: str = "00:00:00:00"
    entries: List[CueEntry] = field(default_factory=list)

    def to_csv(self, path: str) -> bool:
        """写入 CSV cue sheet。

        列：track_name, clip_name, start_tc, end_tc, duration_tc, duration_s, asset, sr, ch
        """
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                # 头部元数据（注释行）
                w.writerow(["# Topos Color Cue Sheet"])
                w.writerow([f"# Project: {self.project_name}"])
                w.writerow([f"# FPS: {self.fps}{' (drop-frame)' if self.drop_frame else ''}"])
                w.writerow([f"# Total Duration: {self.total_duration_tc}"])
                w.writerow([])
                # 列头
                w.writerow([
                    "Track", "Clip", "Start TC", "End TC",
                    "Duration TC", "Duration (s)", "Asset",
                    "Sample Rate", "Channels",
                ])
                for e in self.entries:
                    w.writerow([
                        e.track_name, e.clip_name,
                        e.timeline_start_tc, e.timeline_end_tc,
                        e.duration_tc, f"{e.duration_seconds:.3f}",
                        e.asset_name, e.sample_rate, e.channels,
                    ])
            logger.info(f"Cue sheet written: {path} ({len(self.entries)} entries)")
            return True
        except Exception as e:
            logger.warning(f"Failed to write cue sheet: {e}")
            return False


def build_cue_sheet_from_sequence(
    sequence: "TimelineSequence",
    project_name: str = "",
    sample_rate: int = 48000,
) -> CueSheet:
    """从 TimelineSequence 构建 cue sheet（AM-08 阶段 4.5）。

    列出所有音频轨的 clip，按时间码排列，供外部 DAW 对齐。

    Args:
        sequence: TimelineSequence
        project_name: 工程名
        sample_rate: 采样率

    Returns:
        CueSheet
    """
    fps = getattr(sequence, "fps", 24.0) or 24.0
    # AUD-19101 之后 timeline_v2 的 fps 可能是 Rational——此处全链按
    # float 消费（比较/除法/格式化），入口归一，避免 TypeError。
    fps = float(fps)
    sheet = CueSheet(
        project_name=project_name or getattr(sequence, "name", ""),
        fps=fps,
        total_duration_tc=frames_to_timecode(
            getattr(sequence, "duration_frames", 0), fps,
        ),
    )

    for track in getattr(sequence, "audio_tracks", ()) or ():
        track_name = getattr(track, "name", "") or track.track_id
        for clip in getattr(track, "clips", ()) or ():
            start_frame = getattr(clip, "timeline_start", 0)
            duration = getattr(clip, "timeline_duration", 0)
            end_frame = start_frame + duration
            sheet.entries.append(CueEntry(
                track_name=track_name,
                clip_name=getattr(clip, "clip_id", ""),
                timeline_start_tc=frames_to_timecode(start_frame, fps),
                timeline_end_tc=frames_to_timecode(end_frame, fps),
                duration_tc=frames_to_timecode(duration, fps),
                duration_seconds=duration / fps if fps > 0 else 0.0,
                asset_name=getattr(clip, "asset_id", ""),
                sample_rate=sample_rate,
            ))

    # 按开始时间排序
    sheet.entries.sort(key=lambda e: e.timeline_start_tc)
    return sheet


# ─────────────────────────────────────────────────────────────────────────────
# AAF/OMF/XML 互操作能力评估
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InterchangeFormat:
    """互操作格式描述。"""
    name: str
    extension: str
    available: bool  # 是否已实现/可用
    license: str  # 许可证
    notes: str
    priority: str  # "high"/"medium"/"low"


INTERCHANGE_CAPABILITIES: List[InterchangeFormat] = [
    InterchangeFormat(
        name="BWF (Broadcast Wave)",
        extension=".wav",
        available=True,  # P0-06：bext 实现于 bwf.py，writer 默认注入
        license="无（EBU Tech 3285 标准）",
        notes="EBU Tech 3285 bext chunk：originator/originator_reference/"
              "origination date-time/TimeReference（采样级时间参考）/"
              "coding history，另含 RIFF INFO tag（ICMT/IART/INAM）。"
              "专业交付标准，所有 DAW/NLE 通用。",
        priority="high",
    ),
    InterchangeFormat(
        name="CSV Cue Sheet",
        extension=".csv",
        # F1.3/R4：与注册表 edit.interchange.csv_cue_sheet（DORMANT）对齐——
        # 实现存在但无生产菜单/命令入口，不向用户声明可用。
        available=False,
        license="无（纯文本）",
        notes="列出所有 clip 的 timecode/时长/轨道，供外部 DAW 手动对齐。"
              "实现与测试完备（interchange.py），生产入口待接（见能力注册表）。",
        priority="high",
    ),
    InterchangeFormat(
        name="Audio Stems",
        extension=".wav",
        available=True,  # stem_export.py 实现
        license="无",
        notes="按 D/M/E/Bus 分轨导出 WAV，含 channel layout metadata。",
        priority="high",
    ),
    InterchangeFormat(
        name="Final Cut Pro XML (FCPXML)",
        extension=".fcpxml",
        # Q03（第六轮审计）：与 capability_registry 的 edit.interchange.fcpxml
        # （PARTIAL）对齐——受控子集已接 File 菜单导入/导出生产入口
        # （fcpxml_adapter + import_builder），不再声明不可用。
        available=True,
        license="无（公开 XML schema）",
        notes="Apple Final Cut Pro / DaVinci Resolve 交换格式（受控子集）。"
              "导入带预览确认/映射报告/缺失媒体转离线素材；导出显式列出"
              "丢弃项；multicam/audition/第三方 effect/精细音频 routing "
              "明确降级（详见能力注册表 edit.interchange.fcpxml）。",
        priority="medium",
    ),
    InterchangeFormat(
        name="AAF (Advanced Authoring Format)",
        extension=".aaf",
        # F6.3：验收门未开——aaf2 依赖未引入工程（新增依赖需显式决策）
        # 且"真实接收软件导入重建"验收（Pro Tools/Media Composer）在
        # 本仓环境不可执行。在此之前保持不可用 + 可执行交接建议，
        # 不以 Stem/CSV 冒充可重建时间线（计划原文）。
        available=False,
        license="需 aaf2（BSD-3）或商业 SDK",
        notes="SMPTE 标准，Pro Tools/Media Composer 原生支持。"
              "F6.3 条件延期：依赖与接收端验收门均未开。当前音频后期"
              "交接走 Stems（含 routing.json 分轨重建声明）+ CSV Cue "
              "Sheet（不以 Stem/CSV 冒充可重建"
              "时间线）；需 AAF 时间线重建时经 FCPXML（DaVinci Resolve 可"
              "转出 AAF）或外部工具。",
        priority="low",
    ),
    InterchangeFormat(
        name="OMF (Open Media Framework)",
        extension=".omf",
        available=False,
        license="需 pyomf（有限）或商业工具转换",
        notes="旧格式（已被 AAF 取代）。PyAV 不支持。"
              "评估：不实现，建议用户导出 AAF 或 CSV。",
        priority="low",
    ),
]


def get_available_formats() -> List[InterchangeFormat]:
    """返回已实现的互操作格式。"""
    return [f for f in INTERCHANGE_CAPABILITIES if f.available]


def evaluate_format(name: str) -> Optional[InterchangeFormat]:
    """查询指定格式的评估信息（宽松匹配：前缀/子串/扩展名）。"""
    if not name:
        return None
    n = name.strip().lower()
    for f in INTERCHANGE_CAPABILITIES:
        if f.name.lower() == n or f.extension == n:
            return f
        # 前缀匹配（如 "AAF" 匹配 "AAF (Advanced Authoring Format)"）
        if f.name.lower().startswith(n) or n.startswith(f.name.lower().split("(")[0].strip()):
            return f
    return None
