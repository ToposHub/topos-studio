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
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    entries: list[InterchangeEntry] = []
    diagnostics: list[dict[str, Any]] = []
    sample_rate = int(getattr(sequence, "sample_rate", 48000) or 48000)
    fps_num, fps_den = _fractional_fps(getattr(sequence, "fps", 24))
    audio_tracks = [
        t for t in (getattr(sequence, "audio_tracks", ()) or ())
        if not bool(getattr(t, "is_bus", False))
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
                start = int(clip.timeline_start or 0)
                length = int(clip.timeline_duration or 0)
                if length <= 0:
                    continue
                if start > cursor:
                    filler = create.from_name("Filler")
                    filler.media_kind = SUPPORTED_MEDIA_KIND
                    filler.length = start - cursor
                    components.append(filler)
                elif start < cursor:
                    diagnostics.append({
                        "severity": "warning", "code": "aaf.overlap",
                        "message": (f"clip {clip.clip_id} overlaps previous "
                                    f"by {cursor - start} samples; clamped"),
                    })
                    cursor = start
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
                src_clip.start = int(clip.source_start or 0)
                src_clip.mob = srcmob
                src_clip.slot = src_slot
                components.append(src_clip)
                cursor = start + length
            track_total = int(getattr(sequence, "duration_frames", 0) or 0)
            # 音频轨总长按样本计（48k 参考域模型）
            track_len_samples = max(cursor, track_total)
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
    finally:
        f.close()
    os.replace(tmp_path, output_path)

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
                    asset_key = (str(src_mob.name) if src_mob is not None
                                 else f"mob:{comp_item.mobid}")
                    clip = ClipInstance(
                        clip_id=f"aaf-{asset_key}-{src_start_ref}-{tl_start}",
                        asset_id=asset_key,
                        track_id=f"aaf-slot-{slot.slot_id}",
                        timeline_start=tl_start,
                        timeline_duration=max(1, tl_end - tl_start),
                        source_start=src_start_ref,
                        source_duration=max(1, tl_end - tl_start),
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
