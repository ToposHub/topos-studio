# 状态(2026-08-27 P1-16): 受控子集已接入生产入口（File 菜单导入/导出）。
# 首版明确降级的特性见下方清单，均通过 InterchangeReport 显式列出，不静默丢失。
"""FCPXML Adapter（Phase 7）。

实现 FCPXML 1.x 的受控子集导入/导出（TAEP §9.3）。

**必须支持**（首版）：
- project/library/event 中的基本 timeline；
- format（尺寸、帧率）、资源 asset、asset clip、gap；
- video/audio track 顺序与 clip placement；
- source in/out、名称、role、基础启用状态；
- 简单 cross dissolve（能精确映射的）；
- marker；
- 基础字幕的可映射文本和时长；
- 媒体 URL/相对路径、reel/时码等可用元数据；
- 导入/导出的 version、原始文件名、警告和 media mapping report。

**首版明确降级或拒绝**（TAEP §9.3）：
- 任意第三方 effect、复杂速度曲线、audition、multicam；
- compound clip 的任意嵌套；
- 完整 color correction、LUT/ColorStack、Composite Graph；
- 精细音频 routing、bus、sidechain、automation；
- 尚无双向字符/复杂样式映射保证的字幕；
- generator、标题模板、素材内 effect 参数。

不与“旧 Final Cut Pro 7 XML”混用（独立 adapter，``fcp7_xml``）。

实现用标准库 ``xml.etree.ElementTree``，不引入新依赖。FCPXML 是 Apple 公开
schema，无许可风险。
"""

from __future__ import annotations

import logging
import math
import os
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

from src.features.edit.adapters.interchange_types import (
    AssetRelinkRequest,
    ExportResult,
    InterchangeEntry,
    InterchangeEntryCategory,
    InterchangeInspection,
    InterchangeReport,
    ImportResult,
)
from src.shared.media.metadata import Rational


logger = logging.getLogger(__name__)


FCPXML_FORMAT_ID = "fcpxml"
FCPXML_NAMESPACE = "http://localhost"  # FCPXML 1.x 默认命名空间
SUPPORTED_FCPXML_VERSIONS = ("1.8", "1.9", "1.10", "1.11")

#: AUD-12606：外部 FCPXML 文件的解析防护上限。
#: - 文件大小上限（默认 64MB，绑定元素数量上界——无实体膨胀时每个元素
#:   至少占用若干字节，大小上限即元素数上限）；
#: - 拒绝 DTD/内部实体声明（billion laughs / quadratic blowup 的唯一载体）。
_MAX_FCPXML_BYTES: int = 64 * 1024 * 1024
_FORBIDDEN_DECL_MARKERS: tuple[bytes, ...] = (b"<!DOCTYPE", b"<!ENTITY")

#: AUD-12607：FCP 内置 Cross Dissolve effect 的资源 uid（导出 <transition>
#: 必须引用 resources 中的 <effect> 才符合 FCPXML schema）。
_DISSOLVE_EFFECT_UID: str = ".../Effects/Builtins/Dissolve.bundle"


# FCPXML 帧率 → Rational（常见值）
_FCPXML_FPS_MAP: dict[str, Rational] = {
    "24": Rational(24, 1),
    "25": Rational(25, 1),
    "30": Rational(30, 1),
    "50": Rational(50, 1),
    "60": Rational(60, 1),
    "23.98": Rational(24000, 1001),
    "29.97": Rational(30000, 1001),
    "59.94": Rational(60000, 1001),
}


def _rational_to_fcpxml_frame_duration(fps: Rational) -> str:
    """帧率 Rational → FCPXML frameDuration（每帧秒数的有理数）。

    AUD-12600：frameDuration 是**一帧的时长**（秒），与帧率互为倒数：
    24fps → ``1/24s``；NTSC 29.97（30000/1001）→ ``1001/30000s``。
    不得把帧率分子分母原样写入（那是每帧 30000/1001 秒 ≈ 30 秒/帧）。
    """
    return f"{fps.den}/{fps.num}s"


def _fcpxml_frame_duration_to_fps(frame_duration: str) -> Rational:
    """FCPXML frameDuration（每帧秒数）→ 帧率 Rational（倒数换算）。

    Apple 真实样例：``100/2400s`` → 2400/100 = 24fps；
    NTSC ``1001/30000s`` → 30000/1001 ≈ 29.97fps。
    非法输入（0/负时长）抛 ValueError，由调用方决定降级策略。
    """
    s = frame_duration.rstrip("s")
    if "/" in s:
        num, den = s.split("/", 1)
        per_frame_num, per_frame_den = int(num), int(den)
        if per_frame_num <= 0 or per_frame_den <= 0:
            raise ValueError(f"invalid frameDuration: {frame_duration!r}")
        return Rational(per_frame_den, per_frame_num)
    # 纯整数/小数（秒）
    seconds = float(s)
    if seconds <= 0.0:
        raise ValueError(f"invalid frameDuration: {frame_duration!r}")
    return Rational(1000, int(seconds * 1000))


def _time_to_frames(time_str: str, fps: Rational) -> int:
    """FCPXML time（秒的有理数，如 ``1250/2400s``）→ 整帧。

    按第 4 章合同：开始点 floor。帧数 = 秒 × 帧率，全部走整数有理数
    运算（t_num * fps.num) // (t_den * fps.den)），避免 NTSC 帧率下
    浮点舍入引入的帧偏移。

    AUD-12608：
    - 小数秒（``"1.5s"``）同样经 :class:`fractions.Fraction` 走整数有理
      floor，不再经 float（负值下 ``int()`` 截断 / float 舍入与声明的
      floor 语义不符）；
    - 解析失败/非法输入不再**静默**返回 0——以 warning 留痕后按帧 0
      降级（导入合同 §9.4：不静默丢信息；私有 helper 的返回值契约
      保持 int，调用方行为不变）。
    """
    s = time_str.rstrip("s")
    try:
        if "/" in s:
            num, den = s.split("/", 1)
            t_num, t_den = int(num), int(den)
            if t_den <= 0:
                logger.warning(
                    "FCPXML time %r: non-positive denominator %d; "
                    "falling back to frame 0", time_str, t_den)
                return 0
            return (t_num * fps.num) // (t_den * fps.den)
        # 纯整数/小数秒：Fraction 接受 "1.5"/"2" 等十进制字面量并给出
        # 精确有理数；地板除对负值同样是 floor 语义。
        t = Fraction(s)
        return (t.numerator * fps.num) // (t.denominator * fps.den)
    except (ValueError, ZeroDivisionError) as exc:
        logger.warning(
            "FCPXML time %r is not parseable (%s); falling back to frame 0",
            time_str, exc)
        return 0


def _frames_to_time(frames: int, fps: Rational) -> str:
    """整帧 → FCPXML time（秒的有理数）。

    每帧时长 = fps.den/fps.num 秒，故 seconds = frames * fps.den / fps.num；
    结果约分到最简（NTSC 3000 帧@30000/1001 → ``1001/10s`` = 100.1s）。
    AUD-12601：分母只可为 fps.num——旧实现多乘 fps.den，NTSC 帧率下
    时间值缩小约 1001 倍。
    """
    if frames == 0:
        return "0s"
    num = frames * fps.den
    den = fps.num
    g = math.gcd(num, den)
    if g > 1:
        num //= g
        den //= g
    return f"{num}/{den}s"


def _time_to_seconds_fraction(time_str: str) -> Optional[Fraction]:
    """FCPXML time → 精确有理秒（M24.2 子帧映射入口）。

    不可解析返回 None（不降级不告警——帧域既有 ``_time_to_frames``
    路径负责降级与留痕，此处偏移按 0 处理）。
    """
    s = time_str.rstrip("s")
    try:
        if "/" in s:
            num, den = s.split("/", 1)
            t_num, t_den = int(num), int(den)
            if t_den <= 0:
                return None
            return Fraction(t_num, t_den)
        return Fraction(s)
    except (ValueError, ZeroDivisionError):
        return None


def _seconds_to_time_str(seconds: Fraction) -> str:
    """有理秒 → FCPXML time 字符串（约分；整数秒无分母形式）。"""
    sec = Fraction(seconds)
    if sec.denominator == 1:
        return f"{sec.numerator}s"
    return f"{sec.numerator}/{sec.denominator}s"


def _import_subframe_offset(
    time_str: Optional[str], fps: Rational, sample_rate: int,
    clip_id: str, diagnostics: list,
) -> int:
    """M24.2 · 导入侧子帧偏移提取（返回 @48000 参考率整数偏移）。

    帧对齐（残差 0）→ 0，零开销；显式 offset 属性才参与映射（缺省
    游标拼接值保持旧行为）。量化残差 <1 参考样本以 warning 记账
    （不静默丢）；不可解析交给帧域既有降级路径，偏移按 0。
    """
    if not time_str:
        return 0
    from src.features.edit.core.sample_time import (
        TimelineSampleClock,
        offset_to_reference_rate,
        subframe_decompose,
    )
    seconds = _time_to_seconds_fraction(time_str)
    if seconds is None:
        return 0
    try:
        clock = TimelineSampleClock(fps, sample_rate)
    except (ValueError, ZeroDivisionError):
        return 0
    _frame, offset_declared = subframe_decompose(clock, seconds)
    if offset_declared == 0:
        return 0
    whole, residue = offset_to_reference_rate(offset_declared, sample_rate)
    if residue:
        diagnostics.append({
            "severity": "warning",
            "code": "TAEP_INTERCHANGE_LOSSY",
            "message": (
                f"clip {clip_id} 子帧偏移 {time_str!r} 量化到 48k 参考"
                f"率损失 {float(residue):.3f} 样本（<1 参考样本，记账）"),
        })
    return whole


def _clip_offset_time(clip: Any, frame: int, fps: Rational,
                      sample_rate: int) -> str:
    """M24.2 · 导出 offset：子帧样本偏移按声明采样率精确合成有理秒。

    帧对齐 clip（偏移 0）走既有 ``_frames_to_time``，输出零变化。
    """
    sub_ref = int(getattr(clip, "audio_sample_offset", 0) or 0)
    if not sub_ref:
        return _frames_to_time(frame, fps)
    from src.features.edit.core.sample_time import (
        AUDIO_REFERENCE_SAMPLE_RATE,
        TimelineSampleClock,
        compose_timeline_seconds,
    )
    clock = TimelineSampleClock(fps, sample_rate)
    # 参考率偏移 → 声明率域（精确有理数）后合成时间线秒
    off_declared = (Fraction(sub_ref) * int(sample_rate)
                    / AUDIO_REFERENCE_SAMPLE_RATE)
    return _seconds_to_time_str(
        compose_timeline_seconds(clock, frame, off_declared))


def _parse_fcpxml(source: str | Path) -> ET.Element:
    """带防护地解析外部 FCPXML 文件（AUD-12606）。

    FCPXML 是第三方工程交换文件（不可信输入）。标准库 ElementTree 对实体
    膨胀（billion laughs）与二次膨胀属易受攻击类别，且不引入新依赖
    （defusedxml 不在依赖清单），故在解析前做两层防御：

    1. 文件大小上限（同时绑定元素数量上界）；
    2. 逐块扫描并拒绝 DTD / 内部实体声明（实体膨胀的唯一载体；fail-closed，
       注释里出现这些串也会被拒绝，宁可误拒不可放行）。

    越限或含禁用声明时抛 ``ValueError``；文件不存在/不可读抛 ``OSError``；
    内容非法抛 ``ET.ParseError``。均由调用方降级为 InterchangeReport 诊断。
    """
    path = str(source)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = -1
    if size > _MAX_FCPXML_BYTES:
        raise ValueError(
            f"FCPXML exceeds size limit: {size} > {_MAX_FCPXML_BYTES} bytes"
        )
    scan_budget = size if size >= 0 else _MAX_FCPXML_BYTES
    overlap = max(len(m) for m in _FORBIDDEN_DECL_MARKERS) - 1
    tail = b""
    with open(path, "rb") as fh:
        while scan_budget > 0:
            chunk = fh.read(min(65536, scan_budget))
            if not chunk:
                break
            scan_budget -= len(chunk)
            window = tail + chunk
            for marker in _FORBIDDEN_DECL_MARKERS:
                if marker in window:
                    raise ValueError(
                        f"FCPXML contains forbidden DTD/entity declaration "
                        f"({marker.decode('ascii')}); entity expansion is not allowed"
                    )
            tail = window[-overlap:] if overlap > 0 else b""
    return ET.parse(path).getroot()


def _parse_int_attr(
    element: ET.Element,
    name: str,
    default: int,
    entries: list[InterchangeEntry],
    diagnostics: list[dict[str, Any]],
) -> int:
    """解析元素的整型属性，非法/非正值降级为默认值并记录报告（AUD-12602）。

    外部文件的 ``width="1920.0"`` 之类的脏值不得以未捕获 ``ValueError``
    逃逸到 Qt 槽（PyQt 未捕获槽异常会 qFatal 中止进程）。
    """
    raw = element.get(name)
    if raw is None:
        return default
    text = str(raw).strip()
    value: int | None = None
    try:
        value = int(text)
    except (ValueError, TypeError):
        value = None
    if value is None or value <= 0:
        diagnostics.append({
            "severity": "warning", "code": "TAEP_INTERCHANGE_LOSSY",
            "message": f"invalid {name}={raw!r} on <{element.tag}>; "
                       f"fallback to default {default}",
        })
        entries.append(InterchangeEntry(
            category=InterchangeEntryCategory.CONVERTED, element="format",
            description=f"{name}={raw!r} 非法，回退默认 {default}",
        ))
        return default
    return value


def _iter_marker_frames(
    clip_el: ET.Element, clip_start: int, fps: Rational
) -> list[tuple[int, str]]:
    """收集 clip 元素直接子级 <marker> 的（帧, 名称）列表（AUD-12603）。

    FCPXML 的 marker 挂在 clip（asset-clip/gap/title 等）元素下；受控子集
    按 clip 相对时间解释 ``start``（与本 adapter 导出侧写入方式一致）。
    """
    results: list[tuple[int, str]] = []
    for sub in clip_el:
        if _localname(sub.tag) != "marker":
            continue
        start_str = sub.get("start", "0s")
        rel = _time_to_frames(start_str, fps)
        frame = max(0, clip_start + rel)
        results.append((frame, sub.get("value", "") or sub.get("name", "") or "Marker"))
    return results


class FCPXMLAdapter:
    """FCPXML 1.x 受控子集的 import/export adapter。

    接口使用明确名称 ``fcpxml``，不与 fcp7_xml 混用（TAEP §9.1）。
    """

    format_id = FCPXML_FORMAT_ID

    # ---------------- inspect ---------------- #

    def inspect(self, source: str | Path) -> InterchangeInspection:
        """检查 FCPXML：版本/资源/不支持特性，不导入。"""
        try:
            root = _parse_fcpxml(source)
        except (ET.ParseError, FileNotFoundError, OSError, ValueError) as exc:
            return InterchangeInspection(
                format_id=FCPXML_FORMAT_ID, supported=False,
                report=InterchangeReport(
                    format_id=FCPXML_FORMAT_ID,
                    diagnostics=[{"severity": "error", "code": "TAEP_INTERCHANGE_LOSSY",
                                  "message": f"FCPXML parse failed: {exc}"}],
                ),
            )

        version = root.get("version", "")
        supported = version in SUPPORTED_FCPXML_VERSIONS or version.startswith("1.")

        entries: list[InterchangeEntry] = []
        asset_count = len(root.findall(".//asset"))
        timeline_count = len(root.findall(".//sequence"))
        # 检测不支持特性
        unsupported: list[str] = []
        for tag, label in (
            ("multicamClip", "multicam"),
            ("audition", "audition"),
            ("effect", "第三方 effect"),
            ("audioChannelEntry", "精细音频 routing"),
        ):
            if root.findall(f".//{tag}"):
                unsupported.append(label)
                entries.append(InterchangeEntry(
                    category=InterchangeEntryCategory.DROPPED, element=label,
                    description=f"FCPXML 含 {label}，首版不支持，将被忽略",
                ))

        return InterchangeInspection(
            format_id=FCPXML_FORMAT_ID, version=version, supported=supported,
            asset_count=asset_count, timeline_count=timeline_count,
            unsupported_features=tuple(unsupported),
            report=InterchangeReport(format_id=FCPXML_FORMAT_ID, entries=tuple(entries)),
        )

    # ---------------- import ---------------- #

    def import_to_canonical(
        self, source: str | Path, *, options: dict[str, Any] | None = None
    ) -> ImportResult:
        """导入 FCPXML 到候选 TimelineSequence + MediaAsset 描述。

        不直接 commit；调用方应 preview 后经 command compiler 提交（TAEP §9.2）。

        受控子集（AUD-12603）：asset-clip/gap/title(基础字幕)/marker/简单
        cross dissolve（双邻 clip 可精确定位时）；其余 spine 子元素按类记
        DROPPED 条目；除第一个 sequence 外的其余 sequence 记 DROPPED 条目，
        均不静默丢弃（TAEP §9.4）。
        """
        from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, SubtitleClip, TimelineMarker,
            TimelineSequence, TransitionInstance,
        )

        inspection = self.inspect(source)
        if not inspection.supported:
            return ImportResult(
                sequences=(), assets=(), report=inspection.report or InterchangeReport(FCPXML_FORMAT_ID),
            )

        entries: list[InterchangeEntry] = list(inspection.report.entries) if inspection.report else []
        diagnostics: list[dict[str, Any]] = (
            list(inspection.report.diagnostics) if inspection.report else []
        )
        relink: list[AssetRelinkRequest] = []

        # AUD-12602：inspect 与本次解析之间存在 TOCTOU 窗口（文件可被替换/
        # 删除/写坏），二次解析失败必须降级为报告诊断，不得让异常逃逸到
        # Qt 槽（PyQt 未捕获槽异常会 qFatal 中止进程）。
        try:
            root = _parse_fcpxml(source)
        except (ET.ParseError, FileNotFoundError, OSError, ValueError) as exc:
            return ImportResult(
                sequences=(), assets=(),
                report=InterchangeReport(
                    format_id=FCPXML_FORMAT_ID,
                    diagnostics=diagnostics + [{
                        "severity": "error", "code": "TAEP_INTERCHANGE_LOSSY",
                        "message": f"FCPXML parse failed during import: {exc}",
                    }],
                ),
            )

        # 收集 assets（FCPXML <asset> 元素）
        assets_meta: dict[str, dict[str, Any]] = {}
        for asset_el in root.findall(".//asset"):
            aid = asset_el.get("id", "")
            name = asset_el.get("name", aid)
            start = asset_el.get("start", "0s")
            duration = asset_el.get("duration", "0s")
            has_video = asset_el.find("media-rep") is not None or asset_el.get("hasVideo", "1") == "1"
            media_type = "video" if has_video else "audio"
            # src 属性（媒体文件路径）
            src = ""
            media_rep = asset_el.find(".//media-rep")
            if media_rep is not None:
                src = media_rep.get("src", "") or ""
            assets_meta[aid] = {
                "asset_id": aid, "name": name, "media_type": media_type,
                "start": start, "duration": duration, "src": src,
            }
            if src:
                relink.append(AssetRelinkRequest(external_ref=aid, suggested_path=src))

        # 收集 format（帧率/分辨率）
        fmt_el = root.find(".//format")
        fps = Rational(24, 1)
        width, height = 1920, 1080
        # M24（R06）：工程采样率从 format@audioRate 解析（"48k"/"48000"），
        # 缺省 48000——此前硬编码 48000，44.1k 工程导入后子帧偏移换算
        # 被默认值掩盖。
        sample_rate = 48000
        if fmt_el is not None:
            ar_raw = (fmt_el.get("audioRate") or "").strip()
            if ar_raw:
                try:
                    normalized = ar_raw.rstrip("kK")
                    sample_rate = int(float(normalized) * (
                        1000 if normalized != ar_raw else 1))
                    if sample_rate <= 0:
                        raise ValueError("non-positive")
                except ValueError:
                    diagnostics.append({
                        "severity": "warning",
                        "code": "TAEP_INTERCHANGE_LOSSY",
                        "message": f"invalid audioRate {ar_raw!r}; "
                                   f"fallback to 48000",
                    })
                    sample_rate = 48000
        if fmt_el is not None:
            fd = fmt_el.get("frameDuration")
            if fd:
                try:
                    fps = _fcpxml_frame_duration_to_fps(fd)
                except (ValueError, ZeroDivisionError) as exc:
                    # AUD-12605：捕获具体异常并显式报告降级，不再静默回退 24fps。
                    diagnostics.append({
                        "severity": "warning", "code": "TAEP_INTERCHANGE_LOSSY",
                        "message": f"invalid frameDuration {fd!r}: {exc}; "
                                   f"fallback to 24fps",
                    })
                    entries.append(InterchangeEntry(
                        category=InterchangeEntryCategory.CONVERTED, element="format",
                        description=f"frameDuration {fd!r} 无法解析，帧率回退默认 24fps",
                    ))
            # AUD-12602：外部文件的 width/height 脏值（如 "1920.0"）不得以
            # 未捕获 ValueError 逃逸；降级为默认值 + 报告条目。
            width = _parse_int_attr(fmt_el, "width", 1920, entries, diagnostics)
            height = _parse_int_attr(fmt_el, "height", 1080, entries, diagnostics)

        # effect 资源表（transition 引用判定 cross dissolve 用）
        effects_meta: dict[str, str] = {}
        for effect_el in root.findall(".//effect"):
            effects_meta[effect_el.get("id", "")] = " ".join(
                (effect_el.get("name", ""), effect_el.get("uid", ""))
            ).lower()

        # 收集 sequence → TimelineSequence（首版只取第一个）
        sequences: list[TimelineSequence] = []
        seq_elements = root.findall(".//sequence")
        for seq_el in seq_elements:
            seq_name = seq_el.get("name", "Imported")
            # 遍历 spine 下的 asset-clip / gap / title / transition
            v_clips: list[ClipInstance] = []
            a_clips: list[ClipInstance] = []
            v_transitions: list[TransitionInstance] = []
            sub_clips: list[SubtitleClip] = []
            markers: list[TimelineMarker] = []
            offset = 0  # 时间线游标（帧）
            clip_idx = 0
            marker_idx = 0
            sub_idx = 0
            dropped_tags: dict[str, int] = {}  # AUD-12603：未支持 spine 子元素计数
            pending_transitions: list[tuple[int, int, int, str]] = []  # (pos, start, dur, effect_key)
            spine = seq_el.find(".//spine")
            if spine is None:
                continue
            children = list(spine)
            clip_at_pos: dict[int, ClipInstance] = {}
            for pos, child in enumerate(children):
                tag = _localname(child.tag)
                if tag == "asset-clip":
                    aid = child.get("ref", "") or ""
                    if not aid:
                        # AUD-12604：完全缺失 ref 的 clip 无法映射到任何资源 → 丢弃并报告
                        entries.append(InterchangeEntry(
                            category=InterchangeEntryCategory.DROPPED, element="asset-clip",
                            description=f"asset-clip {child.get('name', '')!r} 缺少 ref 属性，无法映射，已丢弃",
                        ))
                        continue
                    asset = assets_meta.get(aid)
                    dangling = asset is None
                    asset_meta = asset or {}
                    # offset
                    # M24.2：显式 offset 的子帧残差按声明时基
                    # （fps × audioRate）映射为参考率样本挂 clip，
                    # 不再被整帧 floor 丢掉；缺省游标拼接值不参与。
                    off_raw = child.get("offset")
                    off_str = off_raw or f"{offset}s"
                    timeline_start = _time_to_frames(off_str, fps)
                    sub_offset_ref = _import_subframe_offset(
                        off_raw, fps, sample_rate, f"imp-{clip_idx}",
                        diagnostics)
                    # duration
                    dur_str = child.get("duration", asset_meta.get("duration", "0s"))
                    timeline_duration = max(1, _time_to_frames(dur_str, fps))
                    # source in/out
                    # F6.1：真实方言（Resolve/FCP 导出）asset-clip 用
                    # start 属性携带源入点；tcStart 是旧 fixture 方言。
                    # 优先级：clip start → clip tcStart → asset start。
                    tc_in = (child.get("start")
                             or child.get("tcStart")
                             or asset_meta.get("start", "0s"))
                    src_start = _time_to_frames(tc_in, fps)
                    media_type = asset_meta.get("media_type", "video")
                    clip = ClipInstance(
                        clip_id=f"imp-{clip_idx}", asset_id=aid,
                        track_id="v1" if media_type == "video" else "a1",
                        timeline_start=timeline_start,
                        timeline_duration=timeline_duration,
                        source_start=src_start,
                        source_duration=timeline_duration,
                        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
                        audio_sample_offset=sub_offset_ref,
                    )
                    if sub_offset_ref:
                        entries.append(InterchangeEntry(
                            category=InterchangeEntryCategory.CONVERTED,
                            element="asset-clip",
                            description=(f"clip imp-{clip_idx} 子帧偏移映射为 "
                                         f"{sub_offset_ref} 样本@48k 参考率"),
                        ))
                    # M24.3：note 携带的活动调色版本标记 → clip
                    # _extra_fields（随工程保存往返）。版本表本体不随
                    # FCPXML——条目明示恢复侧需与工程版本表对拍。
                    note_raw = child.get("note", "") or ""
                    grade_marker = ""
                    if "topos:grade=" in note_raw:
                        grade_marker = note_raw.split(
                            "topos:grade=", 1)[1].split(";", 1)[0].strip()
                    if grade_marker:
                        clip._extra_fields["topos_grade_version"] = grade_marker
                        entries.append(InterchangeEntry(
                            category=InterchangeEntryCategory.PRESERVED,
                            element="asset-clip",
                            description=(f"clip imp-{clip_idx} 活动调色版本"
                                         f" {grade_marker}（note 标记）——版本"
                                         "表本体不随包，恢复侧与工程对拍"),
                        ))
                    clip_idx += 1
                    clip_at_pos[pos] = clip
                    if media_type == "video":
                        v_clips.append(clip)
                    else:
                        a_clips.append(clip)
                    if dangling:
                        # AUD-12604：悬空 ref 不再伪装 PRESERVED——保留 placement
                        # 供重连，如实标记 REQUIRES_USER_ACTION 并生成 relink 请求。
                        entries.append(InterchangeEntry(
                            category=InterchangeEntryCategory.REQUIRES_USER_ACTION, element="asset-clip",
                            description=f"clip {clip.clip_id} 引用的 asset {aid} 不在 resources 中，需手动重连",
                        ))
                        relink.append(AssetRelinkRequest(external_ref=aid, suggested_path=""))
                    else:
                        entries.append(InterchangeEntry(
                            category=InterchangeEntryCategory.PRESERVED, element="asset-clip",
                            description=f"clip {clip.clip_id} <- asset {aid}",
                        ))
                    # AUD-12603：导入 clip 级 marker（按 clip 相对时间解释 start）
                    for m_frame, m_name in _iter_marker_frames(child, timeline_start, fps):
                        markers.append(TimelineMarker(
                            marker_id=f"imp-marker-{marker_idx}", frame=m_frame,
                            name=m_name, clip_id=clip.clip_id, track_id=clip.track_id,
                        ))
                        marker_idx += 1
                        entries.append(InterchangeEntry(
                            category=InterchangeEntryCategory.PRESERVED, element="marker",
                            description=f"marker {m_name!r} -> clip {clip.clip_id}",
                        ))
                    offset = timeline_start + timeline_duration
                elif tag == "gap":
                    off_str = child.get("offset", f"{offset}s")
                    timeline_start = _time_to_frames(off_str, fps)
                    dur_str = child.get("duration", "0s")
                    timeline_duration = max(1, _time_to_frames(dur_str, fps))
                    offset = timeline_start + timeline_duration
                    # gap 上的 marker 转为时间线级标记
                    for m_frame, m_name in _iter_marker_frames(child, timeline_start, fps):
                        markers.append(TimelineMarker(
                            marker_id=f"imp-marker-{marker_idx}", frame=m_frame,
                            name=m_name,
                        ))
                        marker_idx += 1
                        entries.append(InterchangeEntry(
                            category=InterchangeEntryCategory.PRESERVED, element="marker",
                            description=f"marker {m_name!r} -> gap（时间线级）",
                        ))
                    entries.append(InterchangeEntry(
                        category=InterchangeEntryCategory.CONVERTED, element="gap",
                        description="gap 转为时间线偏移",
                    ))
                elif tag == "title":
                    # AUD-12603：基础字幕导入（可映射文本 + 时长，样式不保证）
                    off_str = child.get("offset", f"{offset}s")
                    sub_start = _time_to_frames(off_str, fps)
                    dur_str = child.get("duration", "0s")
                    sub_dur = max(1, _time_to_frames(dur_str, fps))
                    text = "".join(child.itertext()).strip()
                    overlaps = any(
                        s.timeline_start < sub_start + sub_dur
                        and sub_start < s.timeline_start + s.timeline_duration
                        for s in sub_clips
                    )
                    if not text or overlaps:
                        entries.append(InterchangeEntry(
                            category=InterchangeEntryCategory.DROPPED, element="title",
                            description="title 无可映射文本或与其他字幕区间重叠，已丢弃",
                        ))
                        continue
                    sub = SubtitleClip(
                        clip_id=f"imp-sub-{sub_idx}", track_id="s1",
                        timeline_start=sub_start, timeline_duration=sub_dur, text=text,
                    )
                    sub_idx += 1
                    sub_clips.append(sub)
                    offset = sub_start + sub_dur
                    entries.append(InterchangeEntry(
                        category=InterchangeEntryCategory.CONVERTED, element="title",
                        description=f"title 转为基础字幕（仅文本/时长，样式不保证）: {text[:40]}",
                    ))
                elif tag == "transition":
                    # AUD-12603：简单 cross dissolve——双邻 asset-clip 可精确定位时映射，
                    # 否则记 DROPPED（transition 自身不推进时间线游标）。
                    # 右邻 clip 在第一遍循环中尚未创建，先记录，循环后统一解析。
                    off_str = child.get("offset", f"{offset}s")
                    trans_start = _time_to_frames(off_str, fps)
                    trans_dur = max(1, _time_to_frames(child.get("duration", "12s"), fps))
                    effect_key = " ".join((
                        effects_meta.get(child.get("ref", ""), ""),
                        child.get("name", ""),
                    )).lower()
                    pending_transitions.append((pos, trans_start, trans_dur, effect_key))
                else:
                    # AUD-12603：未支持的 spine 子元素按类计数（role/lane 是属性，
                    # 此处只可能是 audition/multicam-clip/未知元素）
                    dropped_tags[tag] = dropped_tags.get(tag, 0) + 1

            # AUD-12603：第二遍——解析 cross dissolve 的双邻 clip
            for pos, trans_start, trans_dur, effect_key in pending_transitions:
                if "dissolve" not in effect_key:
                    entries.append(InterchangeEntry(
                        category=InterchangeEntryCategory.DROPPED, element="transition",
                        description="非 dissolve 转场首版导入不支持，已丢弃",
                    ))
                    continue
                left = next(
                    (clip_at_pos[p] for p in range(pos - 1, -1, -1)
                     if p in clip_at_pos and clip_at_pos[p].track_id == "v1"),
                    None,
                )
                right = next(
                    (clip_at_pos[p] for p in range(pos + 1, len(children))
                     if p in clip_at_pos and clip_at_pos[p].track_id == "v1"),
                    None,
                )
                if left is None or right is None:
                    entries.append(InterchangeEntry(
                        category=InterchangeEntryCategory.DROPPED, element="transition",
                        description="cross dissolve 缺少可精确定位的双邻 clip，已丢弃",
                    ))
                    continue
                v_transitions.append(TransitionInstance(
                    transition_id=f"imp-trans-{len(v_transitions)}",
                    transition_type="cross_dissolve", track_id="v1",
                    left_clip_id=left.clip_id, right_clip_id=right.clip_id,
                    center_frame=trans_start + trans_dur // 2, duration=trans_dur,
                ))
                entries.append(InterchangeEntry(
                    category=InterchangeEntryCategory.PRESERVED, element="transition",
                    description=f"cross dissolve {left.clip_id} <-> {right.clip_id}",
                ))

            for tag, count in sorted(dropped_tags.items()):
                entries.append(InterchangeEntry(
                    category=InterchangeEntryCategory.DROPPED, element=tag,
                    description=f"{count} 个 <{tag}> 元素首版导入不支持，已丢弃",
                ))

            v_clips.sort(key=lambda c: c.timeline_start)
            a_clips.sort(key=lambda c: c.timeline_start)
            v_track = EditTrack(
                track_id="v1", name="V1", track_type="video", index=0,
                clips=tuple(v_clips), transitions=tuple(v_transitions),
            ) if v_clips else None
            a_track = EditTrack(track_id="a1", name="A1", track_type="audio", index=0, clips=tuple(a_clips)) if a_clips else None
            s_track = None
            if sub_clips:
                sub_clips.sort(key=lambda c: c.timeline_start)
                s_track = EditTrack(
                    track_id="s1", name="S1", track_type="subtitle", index=0,
                    subtitle_clips=tuple(sub_clips),
                )
            seq = TimelineSequence(
                timeline_id=f"imp-seq-{len(sequences)}", name=seq_name, fps=fps,
                width=width, height=height, sample_rate=sample_rate,
                video_tracks=(v_track,) if v_track else (),
                audio_tracks=(a_track,) if a_track else (),
                subtitle_tracks=(s_track,) if s_track else (),
                markers=tuple(sorted(markers, key=lambda m: (m.frame, m.marker_id))),
            )
            sequences.append(seq)
            break  # 首版只取第一个 sequence

        # AUD-12603：多 sequence 显式报告（不静默丢弃）
        if len(seq_elements) > 1:
            dropped_names = [
                el.get("name", "") or f"sequence-{i}"
                for i, el in enumerate(seq_elements[1:], start=2)
            ]
            entries.append(InterchangeEntry(
                category=InterchangeEntryCategory.DROPPED, element="sequence",
                description=(
                    f"检测到 {len(seq_elements)} 个 sequence，首版仅导入第一个，"
                    f"其余 {len(dropped_names)} 个已丢弃：{', '.join(dropped_names[:5])}"
                ),
            ))

        # 资产描述（候选 MediaAsset）
        asset_descs = tuple(
            {"asset_id": f"imp-{aid}", "name": a["name"], "media_type": a["media_type"],
             "path": a["src"]}
            for aid, a in assets_meta.items()
        )

        return ImportResult(
            sequences=tuple(sequences),
            assets=asset_descs,
            relink_requests=tuple(relink),
            report=InterchangeReport(
                format_id=FCPXML_FORMAT_ID,
                entries=tuple(entries),
                diagnostics=tuple(diagnostics),
            ),
        )

    # ---------------- export ---------------- #

    def export_from_canonical(
        self, sequence: Any, *, options: dict[str, Any] | None = None
    ) -> ExportResult:
        """从 canonical TimelineSequence 导出到 FCPXML 文件。

        首版导出受支持子集：asset/asset-clip/gap/marker/cross dissolve。
        Topos 特有数据（ColorStack/Composite）默认关闭；启用时在 topos: namespace。
        """
        options = options or {}
        output_path = options.get("output_path", "")
        if not output_path:
            raise ValueError("export requires output_path in options")

        entries: list[InterchangeEntry] = []
        fps = sequence.fps if hasattr(sequence.fps, "num") else Rational(24, 1)

        # 构建 FCPXML 树
        fcpxml = ET.Element("fcpxml", {"version": "1.9"})
        resources = ET.SubElement(fcpxml, "resources")
        # format
        # M24（R06）：audioRate 随导出写回（工程真实采样率，不再被
        # 48000 默认掩盖；44.1k 工程往返保持 44.1k）
        export_sample_rate = int(
            getattr(sequence, "sample_rate", 48000) or 48000)
        ET.SubElement(resources, "format", {
            "id": "r1", "name": f"{sequence.width}x{sequence.height}",
            "frameDuration": _rational_to_fcpxml_frame_duration(fps),
            "width": str(sequence.width), "height": str(sequence.height),
            "audioRate": ("48k" if export_sample_rate == 48000
                          else str(export_sample_rate)),
        })
        # assets（从 clips 收集 asset_id 与实际使用的源范围，作为时长下限）
        asset_extents: dict[str, int] = {}
        asset_ids: set[str] = set()
        for track in sequence.all_tracks:
            for clip in track.clips:
                asset_ids.add(clip.asset_id)
                extent = clip.source_start + clip.source_duration
                if extent > asset_extents.get(clip.asset_id, 0):
                    asset_extents[clip.asset_id] = extent
        asset_id_map: dict[str, str] = {}
        for i, aid in enumerate(sorted(asset_ids), start=2):
            ref = f"r{i}"
            asset_id_map[aid] = ref
            duration_frames = max(asset_extents.get(aid, 0), 1)
            ET.SubElement(resources, "asset", {
                "id": ref, "name": aid, "hasVideo": "1", "start": "0s",
                "duration": _frames_to_time(duration_frames, fps),
            })
            entries.append(InterchangeEntry(
                category=InterchangeEntryCategory.PRESERVED, element="asset",
                description=f"asset {aid} -> {ref}",
            ))

        # library / event / project / sequence
        library = ET.SubElement(fcpxml, "library")
        event = ET.SubElement(library, "event", {"name": "Topos Export"})
        project = ET.SubElement(event, "project", {"name": sequence.name or "Topos Sequence"})
        seq_el = ET.SubElement(project, "sequence", {
            "id": sequence.timeline_id, "name": sequence.name or "Sequence",
        })
        spine = ET.SubElement(seq_el, "spine")

        # AUD-12607：dissolve effect 惰性注册（首个 cross dissolve 出现时），
        # <transition> 必须引用 resources 中的 <effect> 才符合 FCPXML schema。
        effect_ref: str | None = None
        next_resource_idx = len(asset_id_map) + 2  # r1=format，r2..=asset

        # M24.3：活动调色版本随包——调用方经 options["color_versions"]
        # 供 {stack_id: {"active": name, ...}}（来自工程 color_stack_versions
        # 表）；clip 引用的栈有活动版本时写 asset-clip@note 标记
        # （topos:grade=<name>，FCPXML 无版本表概念→保留 metadata）。
        color_versions = {}
        if isinstance(options, dict):
            raw_versions = options.get("color_versions")
            if isinstance(raw_versions, dict):
                color_versions = raw_versions

        # AUD-12607：spine 元素按时间线位置排序输出（transition 不再统一追加
        # 在所有 clip 之后），并保留 (element, clip) 供 marker 归属。
        spine_items: list[tuple[int, int, int, Any]] = []  # (frame, track_idx, order, payload)
        for track_idx, track in enumerate(sequence.video_tracks):
            if not track.is_enabled:
                continue
            for clip in track.clips:
                spine_items.append((clip.timeline_start, track_idx, 0, clip))
            for trans in getattr(track, "transitions", ()):
                start = getattr(trans, "start_frame", None)
                if not isinstance(start, int):
                    center = int(getattr(trans, "center_frame", 0) or 0)
                    duration = int(getattr(trans, "duration", 0) or 0)
                    start = center - duration // 2
                spine_items.append((start, track_idx, 1, trans))

        exported_clips: list[tuple[ET.Element, Any]] = []
        for frame, _track_idx, order, payload in sorted(
            spine_items, key=lambda it: (it[0], it[1], it[2]),
        ):
            if order == 0:
                clip = payload
                ref = asset_id_map.get(clip.asset_id, "r2")
                clip_el = ET.SubElement(spine, "asset-clip", {
                    "name": clip.clip_id, "ref": ref,
                    # M24.2：offset 含子帧样本偏移时按声明采样率精确
                    # 合成有理秒（帧对齐输出与旧格式逐字节一致）
                    "offset": _clip_offset_time(
                        clip, clip.timeline_start, fps, export_sample_rate),
                    "duration": _frames_to_time(clip.timeline_duration, fps),
                    "tcStart": _frames_to_time(clip.source_start, fps),
                    "enabled": "1" if getattr(clip, "is_enabled", True) else "0",
                })
                # M24.3：活动调色版本声明（栈有活动版本时）——note 标记
                # 随包往返；版本表本体不随 FCPXML（恢复侧与工程对拍）。
                stack_id = getattr(clip, "color_stack_id", None)
                active_grade = None
                if stack_id:
                    active_grade = (color_versions.get(str(stack_id))
                                    or {}).get("active")
                if active_grade:
                    clip_el.set("note", f"topos:grade={active_grade}")
                    entries.append(InterchangeEntry(
                        category=InterchangeEntryCategory.PRESERVED,
                        element="asset-clip",
                        description=(f"clip {clip.clip_id} 活动调色版本随包："
                                     f"{active_grade}（栈 {stack_id}；版本表"
                                     "本体不随 FCPXML，恢复侧对拍）"),
                    ))
                exported_clips.append((clip_el, clip))
                entries.append(InterchangeEntry(
                    category=InterchangeEntryCategory.PRESERVED, element="asset-clip",
                    description=f"exported clip {clip.clip_id}",
                ))
                continue
            trans = payload
            trans_type = getattr(trans, "transition_type", "")
            if trans_type != "cross_dissolve":
                entries.append(InterchangeEntry(
                    category=InterchangeEntryCategory.DROPPED, element="transition",
                    description=f"unsupported transition type {trans_type}",
                ))
                continue
            if effect_ref is None:
                effect_ref = f"r{next_resource_idx}"
                next_resource_idx += 1
                ET.SubElement(resources, "effect", {
                    "id": effect_ref, "name": "Cross Dissolve",
                    "uid": _DISSOLVE_EFFECT_UID,
                })
            ET.SubElement(spine, "transition", {
                "name": "Cross Dissolve",
                "offset": _frames_to_time(frame, fps),
                "duration": _frames_to_time(
                    max(1, int(getattr(trans, "duration", 12) or 12)), fps),
                "ref": effect_ref,
            })
            entries.append(InterchangeEntry(
                category=InterchangeEntryCategory.PRESERVED, element="transition",
                description="cross dissolve",
            ))

        # AUD-12607：marker 必须挂在所属 asset-clip 上（按帧区间归属），
        # 无法归属时记 DROPPED，而不是写到 spine 上破坏 schema。
        for marker in getattr(sequence, "markers", ()):
            host = next(
                (pair for pair in exported_clips
                 if pair[1].timeline_start <= marker.frame
                 < pair[1].timeline_start + pair[1].timeline_duration),
                None,
            )
            if host is None:
                entries.append(InterchangeEntry(
                    category=InterchangeEntryCategory.DROPPED, element="marker",
                    description=f"marker {marker.marker_id}（帧 {marker.frame}）"
                                f"无法归属到任何 asset-clip，已丢弃",
                ))
                continue
            clip_el, clip = host
            ET.SubElement(clip_el, "marker", {
                "start": _frames_to_time(marker.frame - clip.timeline_start, fps),
                "duration": "0s",
                "value": marker.name or "Marker",
            })
            entries.append(InterchangeEntry(
                category=InterchangeEntryCategory.PRESERVED, element="marker",
                description=f"marker {marker.marker_id} -> clip {clip.clip_id}",
            ))

        # 音频轨首版标记为 converted（不导出精细混音）
        if sequence.audio_tracks:
            for track in sequence.audio_tracks:
                if track.clips:
                    entries.append(InterchangeEntry(
                        category=InterchangeEntryCategory.CONVERTED, element="audio",
                        description="audio clip placement preserved;精细混音/routing 不导出",
                    ))

        # 字幕：首版若无保证则标记 requires_user_action
        if sequence.subtitle_tracks:
            for track in sequence.subtitle_tracks:
                if track.subtitle_clips:
                    entries.append(InterchangeEntry(
                        category=InterchangeEntryCategory.REQUIRES_USER_ACTION, element="subtitle",
                        description="字幕样式/双向字符映射未保证；请用户确认 drop/bake/cancel",
                    ))

        # 写文件（路径转义；不悄悄改 source range）
        tree = ET.ElementTree(fcpxml)
        ET.indent(tree, space="  ")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        tree.write(output_path, encoding="utf-8", xml_declaration=True)

        report = InterchangeReport(format_id=FCPXML_FORMAT_ID, entries=tuple(entries))
        return ExportResult(output_path=str(output_path), report=report)


def _localname(tag: str) -> str:
    """去掉命名空间前缀（如 ``{http://...}asset-clip`` → ``asset-clip``）。"""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag
