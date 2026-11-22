"""AAF 音频子集 adapter（B12 · 最小可交接子集，TAEP §9 协议）。

依赖 ``pyaaf2``（锁定 1.7.1；LICENSE=MIT，src/features/edit/core/interchange.py
的旧注释"BSD-3"已核实并更正——B12 步骤 1 要求锁定版本许可核实）。

v1 明确子集（交付对象是音频后期交接，不支持的不写不装）：

- CompositionMob = 一个 TimelineSequence；每条非 Bus 音频轨一个
  TimelineMobSlot（media_kind=Sound，**edit_rate = 序列采样率**——音频
  长度优先样本编辑率，轨内长度单位=样本）。
- 轨内容 = Sequence[SourceClip | Filler]：SourceClip.start = 源内起点
  （样本）、length = timeline_duration（样本）；Filler = 轨内间隙。
- 每 asset 一个 SourceMob（PCMDescriptor：Channels/SampleRate/Length/
  QuantizationBits/BlockAlign/AverageBPS）；v1 不嵌媒体（external 引用，
  导入方经 relink 重连）。
- 不支持项（Clip FX/淡化/自动化/转场/视频轨）全部进
  ``requires_user_action``/``dropped`` 报告，绝不静默丢。

验收门（interchange.py capability）保持 disabled，直到：两套外部接收端
往返报告 + 本文件失败路径测试齐备（B12 手册"迁移与完成"）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

from src.features.edit.adapters.interchange_types import (
    AssetRelinkRequest,
    ExportResult,
    ImportResult,
    InterchangeAdapter,
    InterchangeEntry,
    InterchangeEntryCategory,
    InterchangeInspection,
    InterchangeReport,
)

logger = logging.getLogger(__name__)

try:  # 可选依赖：缺失时 adapter 明确不可用（不悄悄降级成别的格式）
    import aaf2
    import aaf2.rational

    AAF2_AVAILABLE = True
except ImportError:  # pragma: no cover — 环境无 pyaaf2 时
    aaf2 = None
    AAF2_AVAILABLE = False

FORMAT_ID = "aaf"

#: inspect 深度/对象预算（B12 步骤 3：文件大小、对象数量、嵌套深度预算）
INSPECT_MAX_FILE_BYTES = 512 * 1024 * 1024
INSPECT_MAX_MOBS = 4096
INSPECT_MAX_COMPONENTS_PER_SLOT = 100_000

#: v1 支持的轨类型（仅音频；视频轨进 dropped 报告）
SUPPORTED_MEDIA_KIND = "Sound"


@dataclass(frozen=True)
class AafOptions:
    """AAF adapter 选项（B12 数据合同 v1）。"""

    version: int = 1
    handle_samples: int = 0            # 导出片段源窗外加 handles（0=精确）
    unsupported_policy: str = "report" # report | bake（v1 仅 report，烘焙走 B21）
    media_mode: str = "external"       # v1 仅 external 引用（不嵌 PCM）
    preserve_source_metadata: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "handle_samples": self.handle_samples,
            "unsupported_policy": self.unsupported_policy,
            "media_mode": self.media_mode,
            "preserve_source_metadata": self.preserve_source_metadata,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "AafOptions":
        data = data or {}
        return cls(
            version=int(data.get("version", 1)),
            handle_samples=int(data.get("handle_samples", 0) or 0),
            unsupported_policy=str(data.get("unsupported_policy", "report")),
            media_mode=str(data.get("media_mode", "external")),
            preserve_source_metadata=bool(
                data.get("preserve_source_metadata", True)),
        )


def _require_aaf2() -> None:
    if not AAF2_AVAILABLE:
        raise RuntimeError(
            "AAF adapter requires the 'pyaaf2' dependency "
            "(pinned 1.7.1, MIT licensed); install it to enable AAF import/export")


def _R(num: int, den: int = 1):
    return aaf2.rational.AAFRational(int(num), int(den))


def _fractional_fps(fps: Any) -> Tuple[int, int]:
    """fps（Rational/float）→ (num, den) 有理对。"""
    if hasattr(fps, "numerator") and hasattr(fps, "denominator"):
        return int(fps.numerator), int(fps.denominator)
    if hasattr(fps, "num") and hasattr(fps, "den"):
        return int(fps.num), int(fps.den)
    value = float(fps)
    # 常见帧率的有理还原（23.976/29.97）
    rounded = round(value * 1001)
    if abs(rounded / 1001.0 - value) < 1e-6:
        return rounded, 1001
    return int(round(value)), 1


# ─────────────────────────────────────────────────────────────────────────────
# 导出
# ─────────────────────────────────────────────────────────────────────────────

def export_aaf(sequence: Any, output_path: str | Path, *,
               options: Optional[AafOptions] = None) -> ExportResult:
    """TimelineSequence → AAF（音频子集 v1）。

    输出写临时文件成功后原子改名到 output_path（B12 步骤 6：临时目录
    发布语义）。unsupported_policy="bake" 在 v1 明确拒绝（烘焙合同由
    B21 提供，不伪造已烘焙）。
    """
    _require_aaf2()
    options = options or AafOptions()
    if options.unsupported_policy != "report":
        raise ValueError(
            f"unsupported_policy={options.unsupported_policy!r} not "
            "supported in v1 (only 'report'; bake requires B21 contract)")
    # v1 未实现的选项值一律显式拒绝（不静默忽略调用方意图）
    if int(options.version) != 1:
        raise ValueError(f"version={options.version!r} not supported (only 1)")
    if int(options.handle_samples) != 0:
        raise ValueError(
            f"handle_samples={options.handle_samples!r} not supported in v1 "
            "(0 = exact source windows)")
    if str(options.media_mode) != "external":
        raise ValueError(
            f"media_mode={options.media_mode!r} not supported in v1 "
            "(external refs only; embedded essence is not implemented)")
    if not options.preserve_source_metadata:
        raise ValueError("preserve_source_metadata=False not supported in v1")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    from src.features.edit.core.interchange import frames_to_samples as _f2s

    entries: list[InterchangeEntry] = []
    diagnostics: list[dict[str, Any]] = []
    sample_rate = int(getattr(sequence, "sample_rate", 48000) or 48000)
    fps = getattr(sequence, "fps", 24)
    audio_tracks = [
        t for t in (getattr(sequence, "audio_tracks", ()) or ())
        if not bool(getattr(t, "is_bus", False))
    ]
    bus_tracks = [
        t for t in (getattr(sequence, "audio_tracks", ()) or ())
        if bool(getattr(t, "is_bus", False))
    ]
    video_tracks = list(getattr(sequence, "video_tracks", ()) or ())
    if video_tracks:
        entries.append(InterchangeEntry(
            category=InterchangeEntryCategory.DROPPED,
            element="video_tracks",
            description=(f"{len(video_tracks)} video track(s) not written "
                         "(AAF audio-only subset v1)"),
            path="sequence.video_tracks",
        ))
    if bus_tracks:
        entries.append(InterchangeEntry(
            category=InterchangeEntryCategory.DROPPED,
            element="bus_tracks",
            description=(f"{len(bus_tracks)} bus track(s) not written "
                         "(AAF audio subset v1 carries no bus graph/routing)"),
            path="sequence.audio_tracks[buses]",
        ))

    f = aaf2.open(str(tmp_path), "w")
    try:
        create = f.create
        comp = create.from_name("CompositionMob")
        comp.name = str(getattr(sequence, "name", "") or "Topos Sequence")
        f.content.mobs.append(comp)

        # asset_id → SourceMob 缓存（同 asset 多 clip 复用一个 SourceMob）
        src_cache: Dict[str, Any] = {}
        slot_ids = set()
        for track in audio_tracks:
            clips = list(getattr(track, "clips", ()) or ())
            components: list[Any] = []
            cursor = 0  # 轨内样本游标（timeline 域）
            for clip in sorted(clips, key=lambda c: int(c.timeline_start)):
                frame_start = int(clip.timeline_start or 0)
                frame_dur = int(clip.timeline_duration or 0)
                if frame_dur <= 0:
                    continue
                # 帧域 (帧, 子帧偏移) → 样本域（F3.3 绝对边界）：起点 =
                # 帧起点样本 + 子帧偏移，端点 = 末帧边界样本。帧数值
                # 绝不直接当样本用（24fps 下 2000 倍位移）。
                start = _f2s(frame_start, fps, sample_rate) + max(
                    0, int(getattr(clip, "audio_sample_offset", 0) or 0))
                end = _f2s(frame_start + frame_dur, fps, sample_rate)
                length = end - start
                if length <= 0:
                    diagnostics.append({
                        "severity": "warning", "code": "aaf.zero_length",
                        "message": (f"clip {clip.clip_id} resolves to "
                                    f"{length} samples; skipped"),
                    })
                    continue
                placed = start
                if placed > cursor:
                    filler = create.from_name("Filler")
                    filler.media_kind = SUPPORTED_MEDIA_KIND
                    filler.length = placed - cursor
                    components.append(filler)
                elif placed < cursor:
                    # 线性 Sequence 无法表达重叠（尾首相接是合法模型态）：
                    # 顺序化到前一段之后（入点后移），诊断如实记录位移量。
                    placed = cursor
                    diagnostics.append({
                        "severity": "warning", "code": "aaf.overlap",
                        "message": (
                            f"clip {clip.clip_id} overlaps previous by "
                            f"{cursor - start} samples; linear Sequence "
                            "cannot represent overlap — serialized after "
                            "previous clip (in-point delayed)"),
                    })
                # SourceMob（每 asset 一个）
                asset_id = str(clip.asset_id or "")
                if asset_id not in src_cache:
                    srcmob = _build_source_mob(create, f, asset_id,
                                               sample_rate)
                    src_cache[asset_id] = srcmob
                srcmob = src_cache[asset_id]
                src_slot = srcmob.slots[0]
                src_clip = create.from_name("SourceClip")
                src_clip.media_kind = SUPPORTED_MEDIA_KIND
                src_clip.length = length
                src_clip.start = _f2s(int(clip.source_start or 0), fps,
                                      sample_rate) + max(
                    0, int(getattr(clip, "source_sample_offset", 0) or 0))
                src_clip.mob = srcmob
                src_clip.slot = src_slot
                components.append(src_clip)
                cursor = placed + length
            # 轨总长：duration_frames（帧域）→ 样本域再比较（不可帧/样本
            # 混比——24fps×48k 下 2400 帧 ≠ 2400 样本）。
            track_total_samples = _f2s(
                int(getattr(sequence, "duration_frames", 0) or 0),
                fps, sample_rate)
            track_len_samples = max(cursor, track_total_samples)
            if track_len_samples > cursor:
                filler = create.from_name("Filler")
                filler.media_kind = SUPPORTED_MEDIA_KIND
                filler.length = track_len_samples - cursor
                components.append(filler)

            seq = create.from_name("Sequence")
            seq.media_kind = SUPPORTED_MEDIA_KIND
            for comp_item in components:
                seq.components.append(comp_item)
            slot = create.from_name("TimelineMobSlot",
                                    slot_id=len(slot_ids) + 1)
            slot.name = str(getattr(track, "name", "") or track.track_id)
            slot.segment = seq
            # 音频轨槽编辑率 = 序列采样率（样本编辑率；B12 步骤 2）
            slot.edit_rate = _R(sample_rate)
            slot.origin = 0
            comp.slots.append(slot)
            slot_ids.add(slot.slot_id)
        f.save()
        f.close()
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            f.close()
        except Exception:  # noqa: BLE001 — 关闭失败不掩盖原始异常
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    entries.append(InterchangeEntry(
        category=InterchangeEntryCategory.PRESERVED,
        element="audio_tracks",
        description=(f"{len(audio_tracks)} audio track(s), "
                     f"{len(src_cache)} source mob(s) (external refs)"),
        path="sequence.audio_tracks",
    ))
    entries.append(InterchangeEntry(
        category=InterchangeEntryCategory.REQUIRES_USER_ACTION,
        element="clip_fx_fades_automation",
        description=("Clip FX / fades / automation / transitions are not "
                     "converted in v1; bake via B21 or accept loss"),
        path="sequence.*",
    ))
    report = InterchangeReport(format_id=FORMAT_ID,
                               entries=tuple(entries),
                               diagnostics=tuple(diagnostics))
    return ExportResult(output_path=str(output_path), report=report)


def _build_source_mob(create: Any, f: Any, asset_id: str,
                      sample_rate: int) -> Any:
    """建一个 SourceMob（PCMDescriptor；v1 外部引用，不含 essence）。

    通道数/位深未知时使用保守默认（2ch/24bit）并在导入侧要求 relink
    时以实际文件为准——不把默认值当真实描述。
    """
    srcmob = create.from_name("SourceMob")
    srcmob.name = f"topos:{asset_id}"
    desc = create.from_name("PCMDescriptor")
    desc["SampleRate"].value = _R(sample_rate)
    desc["AudioSamplingRate"].value = _R(sample_rate)
    desc["Channels"].value = 2          # 保守默认：导入方按实际文件修正
    desc["Length"].value = 0            # 未知长度（外部引用）
    desc["QuantizationBits"].value = 24
    desc["BlockAlign"].value = 2 * 3    # 2ch × 24bit/8
    desc["AverageBPS"].value = sample_rate * 2 * 3
    srcmob.descriptor = desc
    src_slot = create.from_name("TimelineMobSlot", slot_id=1)
    src_slot.name = "topos:source"
    src_slot.edit_rate = _R(sample_rate)
    src_seg = create.from_name("SourceClip")
    src_seg.media_kind = SUPPORTED_MEDIA_KIND
    src_seg.length = 0
    src_slot.segment = src_seg
    srcmob.slots.append(src_slot)
    f.content.mobs.append(srcmob)
    return srcmob


# ─────────────────────────────────────────────────────────────────────────────
# inspect / 导入
# ─────────────────────────────────────────────────────────────────────────────

def inspect_aaf(source: str | Path) -> InterchangeInspection:
    """只读检查（B12 步骤 3）：预算内枚举 mobs/slots，不修改项目。"""
    _require_aaf2()
    source = Path(source)
    unsupported: list[str] = []
    diagnostics: list[dict[str, Any]] = []
    if source.stat().st_size > INSPECT_MAX_FILE_BYTES:
        diagnostics.append({
            "severity": "error", "code": "aaf.size_budget",
            "message": f"file exceeds inspect budget ({INSPECT_MAX_FILE_BYTES}B)",
        })
        return InterchangeInspection(
            format_id=FORMAT_ID, supported=False,
            unsupported_features=("oversized",), report=InterchangeReport(
                format_id=FORMAT_ID, entries=(),
                diagnostics=tuple(diagnostics)))
    f = aaf2.open(str(source))
    try:
        composition_mobs = list(f.content.compositionmobs())
        master_mobs = list(f.content.mastermobs())
        source_mobs = list(f.content.sourcemobs())
        if len(composition_mobs) + len(master_mobs) + len(source_mobs) \
                > INSPECT_MAX_MOBS:
            diagnostics.append({
                "severity": "error", "code": "aaf.mob_budget",
                "message": f"mob count exceeds inspect budget "
                           f"({INSPECT_MAX_MOBS})",
            })
            unsupported.append("oversized")
        timeline_count = len(composition_mobs) + len(master_mobs)
        for mob in composition_mobs[:32]:
            for slot in mob.slots:
                kind = slot.media_kind
                if kind != SUPPORTED_MEDIA_KIND:
                    unsupported.append(f"slot_media_kind:{kind or 'unknown'}")
                seg = slot.segment
                comps = list(getattr(seg, "components", []) or [])
                if len(comps) > INSPECT_MAX_COMPONENTS_PER_SLOT:
                    unsupported.append("oversized_slot")
        return InterchangeInspection(
            format_id=FORMAT_ID, version="AAF v1.1 (pyaaf2)",
            supported=not any(d.get("severity") == "error" for d in diagnostics),
            asset_count=len(source_mobs),
            timeline_count=timeline_count,
            unsupported_features=tuple(sorted(set(unsupported))),
            report=InterchangeReport(format_id=FORMAT_ID, entries=(),
                                     diagnostics=tuple(diagnostics)),
        )
    finally:
        f.close()


def import_aaf(source: str | Path, *,
               options: Optional[AafOptions] = None) -> ImportResult:
    """AAF → 候选 ImportResult（不 commit；调用方 preview 后提交）。

    v1 子集：CompositionMob/MasterMob 的 Sound 槽 → 音频轨；SourceClip
    start/length → source_start/timeline_duration（样本编辑率直接映射，
    非 48k 率的槽按有理换算到序列 48k 参考域——B12 步骤 2：分别转换
    绝对起止点后相减）。非 Sound 槽进 dropped。
    """
    _require_aaf2()
    options = options or AafOptions()
    source = Path(source)
    entries: list[InterchangeEntry] = []
    diagnostics: list[dict[str, Any]] = []
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
    )

    REF_RATE = 48000
    from src.shared.media.metadata import Rational as _Rational
    # 导入序列使用 TimelineSequence 默认帧率（F3.3 帧域 + 子帧样本偏移）
    import_fps = _Rational(24, 1)
    f = aaf2.open(str(source))
    try:
        relink_requests: list[AssetRelinkRequest] = []
        sequences: list[TimelineSequence] = []
        assets: list[dict[str, Any]] = []
        comp_mobs = list(f.content.compositionmobs()) + list(
            f.content.mastermobs())
        if not comp_mobs:
            diagnostics.append({
                "severity": "error", "code": "aaf.no_composition",
                "message": "no CompositionMob/MasterMob found",
            })
            return ImportResult(sequences=(), assets=(), report=InterchangeReport(
                format_id=FORMAT_ID, entries=tuple(entries),
                diagnostics=tuple(diagnostics)))

        for mob in comp_mobs[:8]:
            audio_tracks: list[EditTrack] = []
            slot_index = 0
            for slot in mob.slots:
                if slot.media_kind != SUPPORTED_MEDIA_KIND:
                    entries.append(InterchangeEntry(
                        category=InterchangeEntryCategory.DROPPED,
                        element="mob_slot",
                        description=(f"slot {slot.slot_id} media_kind="
                                     f"{slot.media_kind} not in audio subset"),
                        path=f"{mob.name}/slot:{slot.slot_id}",
                    ))
                    continue
                seg = slot.segment
                comps = list(getattr(seg, "components", []) or [])
                if not comps and type(seg).__name__ in ("SourceClip", "Filler"):
                    # 单 clip 槽（裸 SourceClip/Filler，无 Sequence 包装）是
                    # 真实 AAF 的常见合法形态——直接作为唯一组件，不得因
                    # 无 components 属性静默导入成空轨。
                    comps = [seg]
                if len(comps) > INSPECT_MAX_COMPONENTS_PER_SLOT:
                    diagnostics.append({
                        "severity": "error", "code": "aaf.slot_budget",
                        "message": f"slot {slot.slot_id} exceeds component "
                                   "budget",
                    })
                    continue
                slot_rate = slot.edit_rate
                slot_rate = ((int(slot_rate.numerator), int(slot_rate.denominator))
                             if slot_rate else (REF_RATE, 1))
                clips: list[ClipInstance] = []
                cursor = 0
                for comp_item in comps:
                    kind = type(comp_item).__name__
                    length = int(getattr(comp_item, "length", 0) or 0)
                    if kind == "Filler":
                        cursor += length
                        continue
                    if kind != "SourceClip":
                        entries.append(InterchangeEntry(
                            category=InterchangeEntryCategory.REQUIRES_USER_ACTION,
                            element=kind,
                            description=(f"{kind} not converted in v1 "
                                         "(FX/fade/automation)"),
                            path=f"{mob.name}/slot:{slot.slot_id}",
                        ))
                        cursor += length
                        continue
                    src_start = int(getattr(comp_item, "start", 0) or 0)
                    src_mob = getattr(comp_item, "mob", None)
                    # 源窗映射：槽率 → 48k 参考域（绝对起止分别换算后相减）
                    tl_start = _convert_rate(cursor, slot_rate, (REF_RATE, 1))
                    tl_end = _convert_rate(cursor + length, slot_rate, (REF_RATE, 1))
                    src_start_ref = _convert_rate(src_start, slot_rate, (REF_RATE, 1))
                    length_samples = max(1, tl_end - tl_start)
                    # 48k 样本域 → 模型帧域 (帧, 子帧偏移)（F3.3 二元组）——
                    # 样本数值绝不直接写进帧字段（24fps 下 2000 倍位移）。
                    tl_frame, tl_off = _samples_to_frame_offset(
                        tl_start, import_fps, REF_RATE)
                    src_frame, src_off = _samples_to_frame_offset(
                        src_start_ref, import_fps, REF_RATE)
                    dur_frames = _samples_to_frames_ceil(
                        length_samples, import_fps, REF_RATE)
                    if _frames_to_samples_exact(
                            dur_frames, import_fps, REF_RATE) != length_samples:
                        diagnostics.append({
                            "severity": "info", "code": "aaf.subframe_length",
                            "message": (
                                f"clip length {length_samples} samples "
                                f"quantized up to {dur_frames} frames "
                                "(model duration is frame-domain; sub-frame "
                                "tail handled by source window)"),
                        })
                    asset_key = (str(src_mob.name) if src_mob is not None
                                 else f"mob:{comp_item.mobid}")
                    clip = ClipInstance(
                        clip_id=f"aaf-{asset_key}-{src_start_ref}-{tl_start}",
                        asset_id=asset_key,
                        track_id=f"aaf-slot-{slot.slot_id}",
                        timeline_start=tl_frame,
                        timeline_duration=dur_frames,
                        source_start=src_frame,
                        source_duration=dur_frames,
                        audio_sample_offset=tl_off,
                        source_sample_offset=src_off,
                    )
                    clips.append(clip)
                    if asset_key not in {a["asset_id"] for a in assets}:
                        assets.append({
                            "asset_id": asset_key,
                            "name": asset_key.split(":")[-1],
                            "media_type": "audio",
                            "path": "",   # AAF v1 不嵌媒体 → relink
                        })
                        relink_requests.append(AssetRelinkRequest(
                            external_ref=asset_key))
                    cursor += length
                slot_index += 1
                audio_tracks.append(EditTrack(
                    f"aaf-slot-{slot.slot_id}",
                    str(slot.name or f"Audio {slot_index}"), "audio",
                    slot_index - 1, clips=tuple(clips)))
            sequence = TimelineSequence(
                audio_tracks=tuple(audio_tracks),
                name=str(mob.name or "AAF Import"),
            )
            entries.append(InterchangeEntry(
                category=InterchangeEntryCategory.PRESERVED,
                element="audio_tracks",
                description=(f"{len(audio_tracks)} audio track(s) with "
                             f"{sum(len(t.clips) for t in audio_tracks)} clip(s)"),
                path=mob.name or "",
            ))
            sequences.append(sequence)
        entries.append(InterchangeEntry(
            category=InterchangeEntryCategory.REQUIRES_USER_ACTION,
            element="media_relink",
            description=("AAF v1 carries external references only; "
                         "relink required for playback"),
            path="assets",
        ))
        return ImportResult(
            sequences=tuple(sequences),
            assets=tuple(assets),
            relink_requests=tuple(relink_requests),
            report=InterchangeReport(format_id=FORMAT_ID,
                                     entries=tuple(entries),
                                     diagnostics=tuple(diagnostics)),
        )
    finally:
        f.close()


def _convert_rate(position: int, from_rate: Tuple[int, int],
                  to_rate: Tuple[int, int]) -> int:
    """有理编辑率换算（§20/B12 算法：绝对起止分别转换后相减）。"""
    if from_rate == to_rate:
        return int(position)
    num_p, den_p = from_rate
    value = int(position) * to_rate[0] * den_p
    denom = to_rate[1] * num_p
    # 对称取整
    if value < 0:
        return -((-2 * value + denom) // (2 * denom))
    return (2 * value + denom) // (2 * denom)


def _fps_fraction(fps: Any) -> "Fraction":
    """fps（Rational/Fraction/float）→ 精确有理数。"""
    from fractions import Fraction
    if hasattr(fps, "numerator") and hasattr(fps, "denominator"):
        return Fraction(int(fps.numerator), int(fps.denominator))
    if hasattr(fps, "num") and hasattr(fps, "den"):
        return Fraction(int(fps.num), int(fps.den))
    return Fraction(str(fps))


def _frames_to_samples_exact(frames: int, fps: Any, sample_rate: int) -> int:
    """帧 → 样本（floor；与 interchange.frames_to_samples 同语义）。"""
    value = Fraction(int(frames) * int(sample_rate)) / _fps_fraction(fps)
    return int(value)


def _samples_to_frame_offset(total_samples: int, fps: Any,
                             sample_rate: int) -> Tuple[int, int]:
    """样本 → (帧, 子帧样本偏移)（interchange.samples_to_frames 的逆，
    F3.3 二元组：位置 = 帧起点样本 + 偏移，偏移 ∈ [0, 每帧样本数)）。"""
    frames_value = Fraction(int(total_samples)) * _fps_fraction(fps) \
        / int(sample_rate)
    frame = int(frames_value)
    offset = int(total_samples) - _frames_to_samples_exact(frame, fps,
                                                           sample_rate)
    return frame, offset


def _samples_to_frames_ceil(total_samples: int, fps: Any,
                            sample_rate: int) -> int:
    """样本数 → 覆盖全部样本的最小帧数（向上取整）。"""
    value = Fraction(int(total_samples)) * _fps_fraction(fps) / int(sample_rate)
    return -((-value.numerator) // value.denominator)


class AafAudioAdapter:
    """InterchangeAdapter 协议实现（format_id='aaf'）。"""

    format_id = FORMAT_ID

    def inspect(self, source: str | Path) -> InterchangeInspection:
        return inspect_aaf(source)

    def import_to_canonical(
        self, source: str | Path, *, options: dict[str, Any] | None = None,
    ) -> ImportResult:
        return import_aaf(source, options=AafOptions.from_dict(options))

    def export_from_canonical(
        self, sequence: Any, *, options: dict[str, Any] | None = None,
    ) -> ExportResult:
        opts = options or {}
        output_path = str(opts.get("output_path") or "export.aaf")
        return export_aaf(sequence, output_path,
                          options=AafOptions.from_dict(opts))


__all__ = [
    "AafAudioAdapter",
    "AafOptions",
    "FORMAT_ID",
    "inspect_aaf",
    "import_aaf",
    "export_aaf",
]
