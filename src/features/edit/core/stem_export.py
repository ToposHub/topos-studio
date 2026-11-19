# B2(2026-09-07): 生产入口已接线 — Edit→File→Export Stems 与 Deliver 页
# "Export Stems…" 按钮共用 ui/stem_export_dialog.py（QThread worker 包装）。
"""
Stem Export — 分轨/分组导出（AM-10 阶段 3.5）。

基于已完成的共享 AudioGraphRenderer（阶段 2.1）和 Bus DAG（AM-14），
支持将工程导出为多个 stem 文件（Master / 单轨 / Bus / Dialogue/Music/Effects）。

设计原则（计划表 3.5）：
- 各 stem 与 solo render 相等：每个 stem 用同一 renderer 渲染该 stem 包含的轨道，
  其余轨道静音/不参与（等价于 solo 该组）。
- 复用共享 renderer 的完整节点链（EQ/FX/automation/Bus/master），不另起简化路径。
- Stem 分组用 EditTrack.stem_group 标签（专业影视 D/M/E），或显式 track_id 列表。

支持类型
--------
1. MASTER_STEM: 整个工程混音（= 普通导出的 master）。
2. TRACK_STEM: 单个轨道单独导出（该轨直送 master，不经过其他轨的 sum）。
3. BUS_STEM: 单个 Bus 单独导出（该 Bus 及其上游轨道）。
4. GROUP_STEM: 按 stem_group 标签聚合（如所有 stem_group="dialogue" 的轨道 → Dialogue stem）。

输出
----
每个 stem 写入独立 WAV 文件（PCM s16le/s24le，采样率/声道数与导出配置一致）。
文件命名：<output_dir>/<stem_name>.wav

验证（计划表 3.5）
------------------
对 3 轨 + 2 Bus 工程导出所有 stem；各 stem 与 solo render 相等；
文件 sample rate/channel layout 正确。
"""

from __future__ import annotations

import logging
import math
import os

from src.features.edit.core.interchange import frames_to_samples
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import TimelineSequence

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Stem 类型与规范
# ─────────────────────────────────────────────────────────────────────────────

class StemType(Enum):
    """Stem 输出类型。"""
    MASTER = "master"      # 整个工程混音
    TRACK = "track"        # 单轨
    BUS = "bus"            # 单 Bus（含其上游轨道）
    GROUP = "group"        # 按 stem_group 标签聚合（D/M/E）


@dataclass(frozen=True)
class StemSpec:
    """单个 stem 的导出规范（不可变）。

    Attributes:
        stem_type: stem 类型
        name: stem 名称（用于文件名与显示，如 "Dialogue"）
        track_ids: 该 stem 包含的 track_id 集合。
            - MASTER: 全部轨道（可留空，渲染时取 sequence 全部）
            - TRACK/BUS/GROUP: 指定参与该 stem 的 track_id 列表
        include_master: 是否经过 master stage（volume/mute/limiter）。
            True=完整 master 链（交付用），False=不经 master（原始分轨，适合再混音）。
        filename_override: 去重后的实际文件名（AUD-18217）。None=按 name
            净化生成。同名轨道/组的 stem 由 plan/export 阶段写入带序号
            的唯一文件名，避免互相覆盖；显示名保持不变。
    """
    stem_type: StemType
    name: str
    track_ids: tuple = ()
    include_master: bool = True
    filename_override: Optional[str] = None

    @property
    def filename(self) -> str:
        """文件名安全版本（空格→下划线）。"""
        if self.filename_override:
            return self.filename_override
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.name)
        return f"{safe or 'stem'}.wav"


def dedupe_stem_filenames(specs: List[StemSpec]) -> List[StemSpec]:
    """对同名 stem 的输出文件名追加序号去重（AUD-18217）。

    复制轨道同名、轨道名与组名净化后相同等情况会让多个 stem 写同
    一个 .wav——后写覆盖先写而结果仍记成功，分轨静默丢失。这里保证
    列表内文件名唯一：冲突的后续 stem 依次追加 ``_2``/``_3``…，仅改
    文件名（写入 ``filename_override``），显示名不动。幂等：对已去重
    的列表再跑一遍是无操作。
    """
    used: set = set()
    out: List[StemSpec] = []
    for spec in specs:
        base = spec.filename
        candidate = base
        counter = 2
        while candidate in used:
            stem_part, ext = os.path.splitext(base)
            candidate = f"{stem_part}_{counter}{ext}"
            counter += 1
        if candidate != base:
            logger.warning(
                "Stem filename collision on '%s'; renaming to '%s' "
                "(stem display name unchanged)",
                base, candidate,
            )
            spec = replace(spec, filename_override=candidate)
        used.add(candidate)
        out.append(spec)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Stem 规划：从 sequence 构建 stem 列表
# ─────────────────────────────────────────────────────────────────────────────

def plan_stems_from_sequence(
    sequence: "TimelineSequence",
    include_master: bool = True,
    group_stems: Optional[Tuple[str, ...]] = None,
) -> List[StemSpec]:
    """从 TimelineSequence 规划 stem 导出列表（AM-10 阶段 3.5）。

    默认生成：
    - 1 个 MASTER stem（全部轨道）
    - 每个有 clip 的普通轨 1 个 TRACK stem
    - 每个 Bus 1 个 BUS stem（含其上游）
    - 按 stem_group 聚合的 GROUP stem（dialogue/music/effects 等非空组）

    Args:
        sequence: TimelineSequence
        include_master: stem 是否经 master stage
        group_stems: 显式指定要导出的 group 标签列表，None=自动检测所有非空 stem_group

    Returns:
        StemSpec 列表
    """
    specs: List[StemSpec] = []
    audio_tracks = [t for t in getattr(sequence, "audio_tracks", ()) or ()]

    # 1. MASTER stem
    all_ids = tuple(t.track_id for t in audio_tracks)
    specs.append(StemSpec(
        stem_type=StemType.MASTER, name="Master",
        track_ids=all_ids, include_master=include_master,
    ))

    # 2. TRACK stems（有 clip 的普通轨）
    for t in audio_tracks:
        if getattr(t, "is_bus", False):
            continue
        if len(getattr(t, "clips", ())) == 0:
            continue
        specs.append(StemSpec(
            stem_type=StemType.TRACK, name=t.name or t.track_id,
            track_ids=(t.track_id,), include_master=False,
        ))

    # 3. BUS stems（每个 Bus 含其上游轨道）
    bus_tracks = [t for t in audio_tracks if getattr(t, "is_bus", False)]
    for bus in bus_tracks:
        upstream = _collect_bus_upstream(bus.track_id, audio_tracks)
        ids = (bus.track_id,) + tuple(upstream)
        specs.append(StemSpec(
            stem_type=StemType.BUS, name=bus.name or bus.track_id,
            track_ids=ids, include_master=False,
        ))

    # 4. GROUP stems（按 stem_group 聚合）
    groups: Dict[str, List[str]] = {}
    for t in audio_tracks:
        sg = getattr(t, "stem_group", "") or ""
        if not sg:
            continue
        groups.setdefault(sg, []).append(t.track_id)

    target_groups = set(group_stems) if group_stems else set(groups.keys())
    for sg in sorted(target_groups):
        if sg in groups and groups[sg]:
            label = sg.capitalize()
            specs.append(StemSpec(
                stem_type=StemType.GROUP, name=label,
                track_ids=tuple(groups[sg]), include_master=include_master,
            ))

    # AUD-18217：同名轨道/Bus/组（复制轨同名、轨道名与组标签净化后相同）
    # 会互相覆盖输出文件且均记成功——规划阶段即对文件名去重。
    return dedupe_stem_filenames(specs)


def _collect_bus_upstream(bus_id: str, all_tracks: list) -> List[str]:
    """收集直接/间接路由到 bus_id 的所有普通轨 track_id（BFS）。"""
    # 构建反向索引：bus_id -> [上游 track_id]
    routing: Dict[str, List[str]] = {}
    for t in all_tracks:
        out = getattr(t, "output_bus_id", None)
        if out:
            routing.setdefault(out, []).append(t.track_id)
        for s in getattr(t, "sends", ()) or ():
            target = getattr(s, "target_bus_id", "")
            if target:
                routing.setdefault(target, []).append(t.track_id)

    result: List[str] = []
    visited = {bus_id}
    queue = [bus_id]
    while queue:
        current = queue.pop(0)
        for upstream_id in routing.get(current, []):
            if upstream_id not in visited:
                visited.add(upstream_id)
                # 只包含普通轨（有 clip 的）；上游 Bus 也继续追溯
                result.append(upstream_id)
                queue.append(upstream_id)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Stem 渲染：复用共享 renderer，按 stem 范围过滤轨道
# ─────────────────────────────────────────────────────────────────────────────

def _iter_stem_pcm_chunks(
    sequence: "TimelineSequence",
    stem: StemSpec,
    sample_rate: int = 48000,
    channels: int = 2,
    decode_clip_fn: Optional[Callable] = None,
    sequence_resolver: Optional[Callable] = None,
    asset_lookup: Optional[Callable] = None,
    chunk_frames: Optional[int] = None,
    pre_render_base_fn: Optional[Callable] = None,
    frame_range: Optional[Tuple[int, int]] = None,
) -> Iterator[np.ndarray]:
    """按时间块流式渲染单个 stem 的 PCM（AUD-18216）。

    旧实现把 output + 每个 Bus 一份全长缓冲整体驻留内存
    （2ch/48kHz/2h float32 单缓冲 ≈ 2.7GB），多 Bus 长片直接
    MemoryError。本生成器按 ~1 秒的帧块渲染并逐块 yield，内存占用
    与工程总时长无关；Bus 输入改为逐块临时缓冲（滚动语义）。

    每轨的 EQ/FX/limiter 均为连续状态节点，逐块调用与整段调用
    逐样本等价（与实时播放同一设计）。帧→样本定位沿用
    ``frames_to_samples`` 的精确有理换算；跨块边界的帧块尾部样本
    由 carry 机制落到下一块，保持与整段渲染一致的累加语义。

    Args:
        sequence: TimelineSequence（含轨道/Bus/master 配置）
        stem: StemSpec
        sample_rate: 采样率
        channels: 声道数
        decode_clip_fn: clip 解码回调（None=静音，测试用）
        sequence_resolver/asset_lookup: 嵌套序列展开（可选）
        chunk_frames: 每块帧数（None=约 1 秒）
        frame_range: 选区 (start_frame, end_frame)（None=整片）。选区
            导出按 H02 语义自动加预卷（效果器状态/自动化先收敛再进入
            选区，预卷段裁掉不交付），与成片 _render_audio_master 同模型。

    Yields:
        (channels, n) float32 PCM 块，顺序拼接即完整 stem（长度 =
        选区样本 + 尾音延长，与整片导出契约一致）。
    """
    from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
    from src.features.edit.core.audio_dsp import EffectChain
    from src.features.edit.core.timeline_render_plan import is_track_renderable
    from src.features.edit.core.channel_layout import adapt_samples_for_delivery

    # P1-15：序列 fps 是 Rational（有理精度），守卫/换算统一转 float
    fps = float(getattr(sequence, "fps", 24.0) or 24.0)
    if fps <= 0:
        fps = 24.0
    # D06（第六轮审计）：帧→样本换算必须用 Rational——float 25.0 时
    # int(29/25.0*48000) 受二进制表示截断为 55679（少 1 样本），与成片
    # 导出的精确边界出现逐帧 1 样本错位。
    fps_exact = getattr(sequence, "fps", None)
    if not (hasattr(fps_exact, "num") and hasattr(fps_exact, "den")):
        fps_exact = fps
    start_frame = 0
    end_frame = getattr(sequence, "duration_frames", 0) or 0
    if frame_range is not None:
        try:
            _range_lo, _range_hi = int(frame_range[0]), int(frame_range[1])
        except (TypeError, ValueError):
            _range_lo, _range_hi = 0, -1
        if _range_hi <= _range_lo:
            return  # 空/退化选区：无内容
        start_frame = max(0, _range_lo)
        end_frame = min(end_frame, _range_hi) if end_frame > 0 else _range_hi
    if end_frame <= start_frame:
        return

    # 审计修复：Rational 精确换算（与 timeline_export 的 P1-15 基准一致），
    # 旧 float 秒换算在 29.97/长时长下与导出侧漂移
    total_samples = frames_to_samples(
        max(0, int(end_frame) - int(start_frame)), fps_exact, int(sample_rate))
    if total_samples <= 0:
        return
    _stem_end_before_tail = int(end_frame)

    # P1-02: 嵌套序列音频展开（可选；与实时/导出共用同一纯函数）。
    # 必须先于 stem_ids——MASTER 的"全部轨"要含虚拟轨。
    if asset_lookup is not None:
        from src.features.edit.core.nested_sequence import flatten_nested_audio_tracks

        expanded = flatten_nested_audio_tracks(
            sequence, sequence_resolver, asset_lookup, strict=True,
            frame_range=(feed_start, end_frame),
        )
        if expanded != tuple(getattr(sequence, "audio_tracks", ()) or ()):
            sequence = replace(sequence, audio_tracks=expanded)

    # 该 stem 包含的 track_id 集合
    stem_ids = set(stem.track_ids)
    if not stem_ids:
        # MASTER: 全部
        stem_ids = {t.track_id for t in getattr(sequence, "audio_tracks", ()) or ()}

    # 构建 renderer（从原始 sequence 配置，保持 EQ/FX/routing/master 参数）
    renderer = AudioGraphRenderer(float(sample_rate))
    renderer.configure_from_sequence(sequence)

    # H03 辅助：clip FX 条目 → 链尾音估算（预算用，不建持久链）
    from src.features.edit.core.audio_retime import clip_fx_entries as _cfe

    def _chain_tail_from_entries(entries) -> int:
        try:
            probe = EffectChain(float(sample_rate))
            probe.set_params(entries, sample_rate=float(sample_rate))
            return int(probe.tail_estimate_samples())
        except Exception:
            return 0

    # H03：尾音延伸——stem 输出长度按全路径 FX 尾音预算延长（轨链 + 沿途
    # Bus 链 + Master 链，串联累计）。修前 stem 长度 = 时间线时长：clip
    # Delay 的 200ms 回声落在内容帧之外被整段截断（成片 0.344 / stem 0）。
    _master_tail = renderer.master_tail_estimate_samples()
    _bus_tails = {n.track_id: n.effect_chain.tail_estimate_samples()
                  for n in renderer._nodes.values()
                  if getattr(n, "is_bus", False) and n.effect_chain is not None}

    def _down_bus_tail(bid):
        seen, total = set(), 0
        while bid is not None and bid not in seen:
            seen.add(bid)
            total += _bus_tails.get(bid, 0)
            node = renderer.get_node(bid)
            bid = node.output_bus_id if node else None
        return total

    _tail_max = _master_tail
    for _n in renderer._nodes.values():
        if getattr(_n, "is_bus", False):
            _tail_max = max(_tail_max, _bus_tails.get(_n.track_id, 0)
                            + _down_bus_tail(
                                renderer.get_node(_n.track_id).output_bus_id)
                            + _master_tail)
    for _t in getattr(sequence, "audio_tracks", ()) or ():
        if getattr(_t, "is_bus", False):
            continue
        _tn = renderer.get_node(_t.track_id)
        if _tn is None:
            continue
        _tt = (_tn.effect_chain.tail_estimate_samples()
               if _tn.effect_chain is not None else 0)
        _tail_max = max(_tail_max, _tt + _down_bus_tail(_tn.output_bus_id)
                        + _master_tail)
    # clip 级 FX 链尾音也计入预算（clip Delay 的回声落在内容帧之外）
    for _t in getattr(sequence, "audio_tracks", ()) or ():
        for _c in getattr(_t, "clips", ()) or ():
            _cfx = _cfe(getattr(_c, "effects", ()) or (), None)
            if _cfx:
                _tail_max = max(_tail_max, _chain_tail_from_entries(_cfx))
    # H02（选区预卷）：与成片 _render_audio_master 同一预算语义——
    # 预卷 = min(start_frame, ceil(全路径尾音预算 / spf))。选区起点冷启动
    # 时 Delay/Reverb/包络状态全零（成片复审：直接导出 f4..10 全零），
    # 向前多渲染让状态收敛后进入选区。预算取 PDC 加入前的 _tail_max
    # （PDC 是纯延迟线，只影响尾部 drain，由下方 += pdc 覆盖）。
    _spf_pre = max(1.0, float(sample_rate) / max(1e-6, fps))
    _preroll_tail = _tail_max
    pre_roll_frames = (min(int(start_frame), int(math.ceil(_preroll_tail / _spf_pre)))
                       if start_frame > 0 else 0)
    preroll_samples = (frames_to_samples(pre_roll_frames, fps_exact, int(sample_rate))
                       if pre_roll_frames > 0 else 0)
    feed_start = start_frame - pre_roll_frames
    # P0 · PDC：图级补偿延迟线的总延迟计入预算（延迟内容需 drain 释放）。
    _tail_max += int(renderer.pdc_total_samples())
    if _tail_max > 0:
        _spf = max(1.0, float(sample_rate) / max(1e-6, fps))
        _tail_frames = int(math.ceil(_tail_max / _spf))
        if _tail_frames > 0:
            end_frame = end_frame + _tail_frames
            # 长度按扩展后范围重算（后续 actual/切片都以 total_samples 为界）
            total_samples = frames_to_samples(
                max(0, int(end_frame) - int(start_frame)),
                fps_exact, int(sample_rate))

    audio_tracks = [t for t in getattr(sequence, "audio_tracks", ()) or ()]
    has_solo = any(getattr(t, "is_solo", False) for t in audio_tracks)
    # RMA-04: solo 可听闭包（替代 is_track_renderable 的旧 solo 分支）。
    audible_solo = renderer.compute_audible_solo_closure() if has_solo else set()

    # 过滤：只保留 stem 包含的轨道
    renderable_tracks = []
    for t in audio_tracks:
        if t.track_id not in stem_ids:
            continue
        if getattr(t, "is_bus", False):
            renderable_tracks.append(t)
        elif len(getattr(t, "clips", ())) > 0:
            if is_track_renderable(t, has_solo):
                # RMA-04: 闭包过滤
                if has_solo and t.track_id not in audible_solo:
                    continue
                renderable_tracks.append(t)

    if not renderable_tracks:
        return

    # 预提取 (track, clip, 帧区间) 工作项；渲染循环按时间块切片。
    work: List[Tuple[Any, Any, int, int]] = []
    for track in renderable_tracks:
        if getattr(track, "is_bus", False):
            continue  # Bus 在 bus stage 处理
        for clip in getattr(track, "clips", ()):
            clip_start = getattr(clip, "timeline_start", 0)
            clip_end = clip_start + getattr(clip, "timeline_duration", 0)
            overlap_start = max(clip_start, feed_start)
            overlap_end = min(clip_end, end_frame)
            if overlap_start >= overlap_end:
                continue
            # D05（第六轮审计）：冻结片段无音频进度——成片混音跳过冻结
            # clip，Stem 此前照常解码导致 Master stem 与成片不一致。
            if getattr(clip, "freeze_source_frame", None) is not None:
                continue
            work.append((track, clip, overlap_start, overlap_end))

    if chunk_frames is None:
        chunk_frames = max(1, int(round(fps)))  # ~1 秒/块

    # D05（第六轮审计）：per-clip FX 链与动画采样器——成片导出在
    # clip 求和前应用 clip FX（MX-04），Stem 与成片同一顺序。
    from src.features.edit.core.audio_dsp import EffectChain
    from src.features.edit.core.audio_retime import (
        animated_fx_sampler,
        clip_fx_entries,
    )

    fx_chains: Dict[str, EffectChain] = {}
    fx_samplers: Dict[str, Any] = {}

    def _clip_fx_for(clip: Any, frame_idx: int) -> Optional[EffectChain]:
        clip_id = getattr(clip, "clip_id", "")
        chain = fx_chains.get(clip_id)
        if chain is None and clip_id not in fx_chains:
            entries = clip_fx_entries(getattr(clip, "effects", ()) or (), None)
            chain = None
            if entries:
                chain = EffectChain(float(sample_rate))
                chain.set_params(entries, sample_rate=float(sample_rate))
            fx_chains[clip_id] = chain
        sampler = fx_samplers.get(clip_id, False)
        if sampler is False:
            sampler = animated_fx_sampler(getattr(clip, "effects", ()) or ())
            fx_samplers[clip_id] = sampler
        # D04：动画 FX 参数按当前帧求值（与成片导出同一采样器）
        if chain is not None and sampler is not None:
            try:
                chain.set_params(sampler(frame_idx), sample_rate=float(sample_rate))
            except Exception as e:  # noqa: BLE001 — 参数更新失败沿用上一帧
                logger.debug(
                    "stem export: FX param update failed for clip %s at frame "
                    "%d (%s); keeping previous params", clip_id, frame_idx, e)
        return chain

    # ─────────────────────────────────────────────────────────────────────
    # R03：Stem 复用成片调度语义（计划 2026-11-15 §5 R03）。
    # - MX-04 求和优先：clip 阶段（fade/crossfade/clip-FX/gain）在解码循环
    #   完成，轨阶段（volume/pan/EQ/FX）对**求和后的帧**每帧只跑一次——
    #   多 clip 非线性 FX 不再逐 clip 各自过链（与成片/实时同构）。
    # - Send tap 语义与成片一致：pre_fader 取求和帧（推子前），post_fader
    #   取处理后输出；send mute 只断开该 send。
    # - 侧链：Bus/Master 链引用的外部检源在本块内按帧收集 chunk 级 tap，
    #   bus/master 阶段消费（此前 stem 完全不传 sidechain_taps）。
    # - SC 检源闭包与可听闭包分开：不在本 stem 集合内的检源轨仍渲染供
    #   tap（输出不进 stem 混音/路由，只喂 chunk_sc）。
    # - 尾音：轨链 tail 预算跨块推进（无内容帧按预算喂零），选区内 drain。
    # ─────────────────────────────────────────────────────────────────────
    sc_needed_ids: set = set(renderer.master_sidechain_sources())
    _sc_deps: Dict[str, set] = {}
    for _t in audio_tracks:
        _n = renderer.get_node(_t.track_id)
        _srcs = (_n.effect_chain.sidechain_sources()
                 if (_n is not None and _n.effect_chain is not None) else set())
        sc_needed_ids |= _srcs
        if not getattr(_t, "is_bus", False):
            _sc_deps[_t.track_id] = {s for s in _srcs if s != _t.track_id}

    # SC-only 轨：被本 stem 的轨/Bus/Master 链引用为检源、但不在 stem
    # 集合内的普通轨——照常参与解码与轨阶段（供 tap），输出不入混音。
    _sc_only_ids: set = set()
    for _t in audio_tracks:
        if getattr(_t, "is_bus", False) or _t.track_id in stem_ids:
            continue
        if _t.track_id in sc_needed_ids and len(getattr(_t, "clips", ())) > 0 \
                and is_track_renderable(_t, has_solo):
            _sc_only_ids.add(_t.track_id)
            renderable_tracks.append(_t)
            for _clip in getattr(_t, "clips", ()):
                _cs = getattr(_clip, "timeline_start", 0)
                _ce = _cs + getattr(_clip, "timeline_duration", 0)
                _os, _oe = max(_cs, feed_start), min(_ce, end_frame)
                if _os >= _oe:
                    continue
                if getattr(_clip, "freeze_source_frame", None) is not None:
                    continue
                work.append((_t, _clip, _os, _oe))
    # 检源先于被控轨（与成片导出同一拓扑序；环按原序 + 告警）
    _content_tracks = [t for t in renderable_tracks if not getattr(t, "is_bus", False)]
    if _sc_deps and any(_sc_deps.values()):
        _placed: set = set()
        _ordered: List[Any] = []
        _remaining = list(_content_tracks)
        while _remaining:
            _progressed = False
            for _t in list(_remaining):
                if _sc_deps.get(_t.track_id, set()) <= _placed:
                    _ordered.append(_t)
                    _placed.add(_t.track_id)
                    _remaining.remove(_t)
                    _progressed = True
            if not _progressed:
                _t = _remaining.pop(0)
                logger.warning(
                    "stem export sidechain dependency cycle at track %s; "
                    "falling back to internal detection", _t.track_id)
                _ordered.append(_t)
                _placed.add(_t.track_id)
        _content_tracks = _ordered

    # 跨块状态：每轨尾音预算与单调游标（帧全局升序；块间连续）
    _tail_budget: Dict[str, int] = {}
    _tail_left: Dict[str, int] = {}
    for _t in _content_tracks:
        _n = renderer.get_node(_t.track_id)
        _tail_budget[_t.track_id] = (
            _n.effect_chain.tail_estimate_samples()
            if (_n is not None and _n.effect_chain is not None) else 0)
        _tail_left[_t.track_id] = 0

    # work 按 (SC 序轨序, clip 序) 稳定重排——Phase 1 的求和顺序确定
    _track_order_idx = {t.track_id: i for i, t in enumerate(_content_tracks)}
    work.sort(key=lambda w: (_track_order_idx.get(w[0].track_id, 1 << 30),
                             w[2]))

    from src.features.edit.core.audio_dsp import (
        apply_clip_fade as _apply_clip_fade,
        apply_crossfade_block as _apply_xfade_block,
    )
    from src.features.edit.core.audio_playback import unpack_send

    # carry: 块边界处帧块尾部超出块尾的样本 (绝对样本位, 数据)。
    carry: List[Tuple[int, np.ndarray]] = []

    for f0 in range(feed_start, end_frame, chunk_frames):
        f1 = min(f0 + chunk_frames, end_frame)
        s0 = frames_to_samples(f0 - feed_start, fps_exact, sample_rate)
        s1 = frames_to_samples(f1 - feed_start, fps_exact, sample_rate)
        if s1 <= s0:
            continue
        chunk_len = s1 - s0
        buf = np.zeros((channels, chunk_len), dtype=np.float32)
        bus_bufs: Dict[str, np.ndarray] = {}

        # 先落位上一块 carry 的跨块样本
        pending: List[Tuple[int, np.ndarray]] = []
        for pos, data in carry:
            rel = pos - s0
            if rel < 0:
                continue  # 陈旧条目（理论上不出现），丢弃
            end = min(rel + data.shape[1], chunk_len)
            if end > rel:
                buf[:, rel:end] += data[:, :end - rel]
            if end < rel + data.shape[1]:
                pending.append((pos + (end - rel), data[:, end - rel:]))
        carry = pending

        # ── Phase 1：clip 解码 + clip 阶段 → (track, frame) 求和 ──
        frame_sum: Dict[Tuple[str, int], np.ndarray] = {}
        for track, clip, ovl_start, ovl_end in work:
            frame_lo = max(ovl_start, f0)
            frame_hi = min(ovl_end, f1)
            for frame_idx in range(frame_lo, frame_hi):
                # D06：块长用精确相邻样本边界差（与成片导出同源）。
                out_start = frames_to_samples(frame_idx - feed_start, fps_exact, sample_rate)
                out_next = frames_to_samples(
                    frame_idx + 1 - feed_start, fps_exact, sample_rate)
                block_len = max(0, out_next - out_start)
                if block_len <= 0:
                    continue
                # 解码（调用方提供真实解码，或测试用静音）
                if decode_clip_fn is not None:
                    samples = decode_clip_fn(track, clip, frame_idx, fps, block_len)
                else:
                    samples = np.zeros((channels, block_len), dtype=np.float32)

                synthesized_from_base = False
                if samples is None:
                    # P0：预渲染成品是独立源——源 EOF（改长度步骤烘焙出的
                    # 成品比源长）时由成品直接供块，块存在性不再由源解码
                    # 决定。无成品保持原跳过语义。
                    _pre_edit = getattr(clip, "audio_edit", None)
                    if _pre_edit and pre_render_base_fn is not None:
                        from src.features.edit.core.audio_edit import (
                            clip_block_from_pre_render,
                            requires_pre_render as _requires_pre_render,
                        )
                        if _requires_pre_render(_pre_edit):
                            _base = pre_render_base_fn(clip)
                            if _base is not None:
                                samples = clip_block_from_pre_render(
                                    _pre_edit, _base,
                                    int(round(
                                        (frame_idx - int(getattr(clip, "timeline_start", 0)))
                                        * sample_rate / fps)),
                                    block_len,
                                    sample_rate=int(sample_rate),
                                    channels=channels,
                                )
                                synthesized_from_base = samples is not None
                    if samples is None:
                        continue

                actual = min(samples.shape[-1],
                             (total_samples + preroll_samples) - out_start)
                if actual <= 0:
                    continue

                if synthesized_from_base:
                    # 成品块已应用完整处理栈且为交付声道宽度：跳过源域
                    # 步骤（交付 downmix / 二次栈应用），直接进入 clip 增益。
                    samples = samples[:, :actual]
                else:
                    # P1-09：与时间线导出同源的声道适配。
                    samples = adapt_samples_for_delivery(
                        samples[:, :actual] if samples.ndim == 2 else samples,
                        channels,
                        context=f"stem {getattr(track, 'stem_group', '')} clip {getattr(clip, 'clip_id', '')}",
                    )

                # P0-3: Clip Editor 处理栈（clip 增益/淡化之前；与实时
                # mix_samples、成片导出同一调用序列，parity 锁定）。
                _audio_edit = getattr(clip, "audio_edit", None)
                if (_audio_edit and samples.shape[-1] > 0
                        and not synthesized_from_base):
                    from src.features.edit.core.audio_edit import (
                        apply_clip_audio_edit_block as _apply_audio_edit,
                        requires_pre_render,
                    )
                    # §3.3：预渲染步骤走缓存基底（调用方提供解析回调）。
                    _pre_base = None
                    if requires_pre_render(_audio_edit) and pre_render_base_fn is not None:
                        _pre_base = pre_render_base_fn(clip)
                    samples = _apply_audio_edit(
                        samples,
                        int(round(
                            (frame_idx - int(getattr(clip, "timeline_start", 0)))
                            * sample_rate / fps)),
                        _audio_edit,
                        sample_rate=int(sample_rate),
                        pre_render_base=_pre_base,
                    )

                # C3: 片段级增益（renderer 节点链之前）。
                _clip_gain_db = float(getattr(clip, "audio_gain_db", 0.0) or 0.0)
                if abs(_clip_gain_db) > 1e-9:
                    samples = samples * (10.0 ** (_clip_gain_db / 20.0))

                # MX-04/R03：clip 阶段（fade/crossfade/clip-FX）在求和前
                # 完成——与成片导出同一调用序列。
                samples = _apply_clip_fade(
                    samples,
                    clip_timeline_start=int(getattr(clip, "timeline_start", 0)),
                    clip_timeline_duration=int(getattr(clip, "timeline_duration", 0)),
                    current_frame=frame_idx,
                    fade_in_frames=int(getattr(clip, "fade_in_duration", 0) or 0),
                    fade_out_frames=int(getattr(clip, "fade_out_duration", 0) or 0),
                    fps=fps,
                    sample_rate=float(sample_rate),
                    fade_in_samples_override=getattr(clip, "fade_in_samples", None),
                    fade_out_samples_override=getattr(clip, "fade_out_samples", None),
                    audio_sample_offset=int(getattr(clip, "audio_sample_offset", 0) or 0),
                    fade_curve=getattr(clip, "fade_curve", None),
                    fade_curve_in=getattr(clip, "effective_fade_curve_in", None),
                    fade_curve_out=getattr(clip, "effective_fade_curve_out", None),
                )
                if getattr(track, "transitions", None):
                    from src.features.edit.core.audio_transition_catalog import (
                        audio_curve_for,
                    )
                    for tr in track.transitions:
                        role = None
                        if tr.left_clip_id == clip.clip_id:
                            role = "left"
                        elif tr.right_clip_id == clip.clip_id:
                            role = "right"
                        if role is None:
                            continue
                        curve = audio_curve_for(tr.transition_type)
                        if curve is None:
                            continue
                        samples = _apply_xfade_block(
                            samples,
                            transition_start_frame=int(tr.start_frame),
                            transition_duration_frames=int(tr.duration),
                            current_frame=frame_idx,
                            role=role,
                            curve=str(curve),
                            fps=fps,
                            sample_rate=float(sample_rate),
                        )
                _clip_chain = _clip_fx_for(clip, frame_idx)
                if _clip_chain is not None and _clip_chain.is_active:
                    samples = _clip_chain.process(samples)

                key = (track.track_id, frame_idx)
                frame_buf = frame_sum.get(key)
                if frame_buf is None:
                    frame_buf = np.zeros((channels, block_len), dtype=np.float32)
                    frame_sum[key] = frame_buf
                _n = min(samples.shape[-1], frame_buf.shape[-1])
                frame_buf[:, :_n] += samples[:channels, :_n].astype(
                    np.float32, copy=False)

            # H03：clip FX 尾音——clip 在本 chunk 内结束时按链尾音预算
            # 继续喂零（与成片导出同语义；修前 stem 的 clip Delay 尾音
            # 为 0而成片 0.344，复审 clip_tail_peaks 已复现）。帧紧邻
            # clip 末帧，链状态连续；帧块汇入轨求和（MX-04 语义）。
            _clip_end = int(clip.timeline_start) + int(clip.timeline_duration)
            if f0 <= _clip_end < f1:
                _chain = fx_chains.get(getattr(clip, "clip_id", ""))
                if _chain is not None and _chain.is_active:
                    _ctail = int(_chain.tail_estimate_samples())
                    if _ctail > 0:
                        _spf = max(1.0, sample_rate / max(1e-6, float(fps)))
                        _ctail_frames = int(math.ceil(_ctail / _spf))
                        for _tf in range(_clip_end,
                                         min(f1, _clip_end + _ctail_frames)):
                            _ts = frames_to_samples(
                                _tf - feed_start, fps_exact, sample_rate)
                            _tn = frames_to_samples(
                                _tf + 1 - feed_start, fps_exact, sample_rate)
                            _tbl = _tn - _ts
                            if _tbl <= 0:
                                continue
                            _tblock = _chain.process(
                                np.zeros((channels, _tbl), dtype=np.float32))
                            _key = (track.track_id, _tf)
                            _tbuf = frame_sum.get(_key)
                            if _tbuf is None:
                                _tbuf = np.zeros((channels, _tbl),
                                                 dtype=np.float32)
                                frame_sum[_key] = _tbuf
                            _m = min(_tblock.shape[-1], _tbuf.shape[-1])
                            _tbuf[:, :_m] += _tblock[:channels, :_m].astype(
                                np.float32, copy=False)

        # ── Phase 2：轨阶段（求和帧一次过链）+ 路由 + sends + SC tap ──
        chunk_sc: Dict[str, np.ndarray] = {}
        chunk_sc_until: Dict[str, int] = {}
        for track in _content_tracks:
            node = renderer.get_node(track.track_id)
            _track_frames = sorted(
                f for (tid, f) in frame_sum if tid == track.track_id)
            for frame_idx in range(f0, f1):
                out_start = frames_to_samples(frame_idx - feed_start, fps_exact, sample_rate)
                out_next = frames_to_samples(
                    frame_idx + 1 - feed_start, fps_exact, sample_rate)
                block_len = max(0, out_next - out_start)
                if block_len <= 0:
                    continue
                summed = frame_sum.pop((track.track_id, frame_idx), None)
                if summed is None:
                    if _tail_left.get(track.track_id, 0) > 0:
                        _tail_left[track.track_id] = max(
                            0, _tail_left[track.track_id] - block_len)
                        summed = np.zeros((channels, block_len), dtype=np.float32)
                    else:
                        continue  # 间隙且无尾音 → 冻结（与实时/成片一致）
                else:
                    _tail_left[track.track_id] = _tail_budget.get(
                        track.track_id, 0)
                # R03：轨级外部检源（同块内已处理的检源轨切片）
                _taps = {
                    _src: _buf[:, out_start - s0:out_next - s0]
                    for _src, _buf in chunk_sc.items()
                    if chunk_sc_until.get(_src, 0) >= out_next - s0
                } or None
                processed = renderer.render_track_block(
                    track.track_id, summed, frame_idx, fps=fps,
                    sidechain_taps=_taps)
                if processed is None:
                    continue
                actual = min(processed.shape[-1], block_len,
                             (total_samples + preroll_samples) - out_start)
                if actual <= 0:
                    continue
                proc = processed[:channels, :actual]
                local = out_start - s0
                take = min(actual, chunk_len - local)
                if take > 0 and track.track_id in _sc_only_ids:
                    # SC-only 轨：不入 stem 混音/路由/sends，只喂检源 tap
                    _scbuf = chunk_sc.get(track.track_id)
                    if _scbuf is None:
                        _scbuf = np.zeros((channels, chunk_len), dtype=np.float32)
                        chunk_sc[track.track_id] = _scbuf
                    _scbuf[:, local:local + take] += proc[:, :take]
                    chunk_sc_until[track.track_id] = out_next - s0
                    continue
                if take > 0:
                    target = node.output_bus_id if node else None
                    if target is not None and target in stem_ids:
                        if target not in bus_bufs:
                            bus_bufs[target] = np.zeros(
                                (channels, chunk_len), dtype=np.float32)
                        bus_bufs[target][:, local:local + take] += proc[:, :take]
                    elif stem.stem_type == StemType.BUS:
                        # F3.4：BUS stem 语义 = 该 Bus 的输出；路由到别处/
                        # 直达 Master 的轨只经 send 贡献，干声不落 stem 缓冲。
                        pass
                    else:
                        buf[:, local:local + take] += proc[:, :take]
                    if node is not None:
                        for send_entry in node.sends:
                            # R03/MX-14: pre_fader 取求和帧（推子前），
                            # post_fader 取处理后输出；muted 只断开该 send。
                            send_bus_id, send_level, send_tap, send_muted = (
                                unpack_send(send_entry))
                            if send_level <= 0.0 or send_muted \
                                    or send_bus_id not in stem_ids:
                                continue
                            if send_bus_id not in bus_bufs:
                                bus_bufs[send_bus_id] = np.zeros(
                                    (channels, chunk_len), dtype=np.float32)
                            if send_tap == "pre_fader":
                                pre_take = min(summed.shape[-1], take)
                                bus_bufs[send_bus_id][
                                    :, local:local + pre_take] += \
                                    summed[:channels, :pre_take] * send_level
                            else:
                                bus_bufs[send_bus_id][
                                    :, local:local + take] += \
                                    proc[:, :take] * send_level
                    # R03：检源轨块内 tap（供后续被控轨与 bus/master 阶段）
                    if track.track_id in sc_needed_ids:
                        _scbuf = chunk_sc.get(track.track_id)
                        if _scbuf is None:
                            _scbuf = np.zeros((channels, chunk_len),
                                              dtype=np.float32)
                            chunk_sc[track.track_id] = _scbuf
                        _scbuf[:, local:local + take] += proc[:, :take]
                        chunk_sc_until[track.track_id] = out_next - s0
                if take < actual and f1 < end_frame:
                    carry.append((out_start + take, proc[:, take:]))
        frame_sum.clear()

        # Bus stage（仅本块的 Bus 输入；renderer 自动补零缓冲）
        # RMA-04: 传入可听闭包；RMA-06: 传入块起始帧/fps 让 Bus automation 求值
        # R03: Bus 链外部检源 tap（与成片同一消费入口）
        renderer.render_bus_stage(bus_bufs, buf, has_solo=has_solo,
                                  audible_solo=audible_solo,
                                  start_frame=f0, fps=fps,
                                  sidechain_taps=chunk_sc or None)

        # Master stage（可选；连续状态 limiter 跨块一致）
        # §3.1: 传入块起始帧/fps 供 master automation ramp
        # R03: Master 链外部检源与 Bus 同一 tap 池
        if stem.include_master:
            buf = renderer.apply_master(buf, start_frame=f0, fps=fps,
                                        sidechain_taps=chunk_sc or None)

        # H02: 裁掉预卷段（预卷可跨多个 chunk，按 feed 坐标精确跳过；
        # 整块落在预卷内则整块跳过）
        if preroll_samples > 0:
            skip = int(preroll_samples - s0)
            if skip >= buf.shape[-1]:
                continue
            if skip > 0:
                buf = np.ascontiguousarray(buf[:, skip:])
        yield buf


def render_stem_pcm(
    sequence: "TimelineSequence",
    stem: StemSpec,
    sample_rate: int = 48000,
    channels: int = 2,
    decode_clip_fn: Optional[Callable] = None,
    sequence_resolver: Optional[Callable] = None,
    asset_lookup: Optional[Callable] = None,
    pre_render_base_fn: Optional[Callable] = None,
    frame_range: Optional[Tuple[int, int]] = None,
) -> Optional[np.ndarray]:
    """渲染单个 stem 的完整 PCM（兼容入口；模型/测试用）。

    生产导出路径（export_all_stems）已改为消费
    ``_iter_stem_pcm_chunks`` 的流式块并增量写入 WAV（AUD-18216），
    不再把整条时间线驻留内存。本函数仅供需要整段数组的调用方
    （测试比对、离线分析）使用。

    Args:
        sequence: TimelineSequence（含轨道/Bus/master 配置）
        stem: StemSpec（定义包含哪些 track_id 与是否经 master）
        sample_rate: 采样率
        channels: 声道数
        decode_clip_fn: 可选的 clip 解码回调 (track, clip, frame_idx, fps, n_samples) -> samples|None。
            None 时返回静音（用于纯模型测试）；实际导出由调用方提供真实解码。

    Returns:
        (channels, total_samples) PCM float32，或 None（无内容）
    """
    chunks = list(_iter_stem_pcm_chunks(
        sequence, stem, sample_rate, channels,
        decode_clip_fn=decode_clip_fn,
        sequence_resolver=sequence_resolver,
        asset_lookup=asset_lookup,
        pre_render_base_fn=pre_render_base_fn,
        frame_range=frame_range,
    ))
    if not chunks:
        return None
    if len(chunks) == 1:
        return chunks[0]
    return np.concatenate(chunks, axis=1)


# ─────────────────────────────────────────────────────────────────────────────
# Stem 写入：PCM → WAV 文件
# ─────────────────────────────────────────────────────────────────────────────

class _StemWavWriter:
    """增量 PCM→WAV 写入器（AUD-18216：流式导出配套）。

    使用 PyAV 的 fltp（float planar）格式输入，由编码器量化为目标
    位深（与 video_encoder.write_audio_samples 一致，保证兼容性）。
    分块 feed，写完 close；导出方无需整段 PCM 驻留内存。
    """

    def __init__(
        self,
        path: str,
        sample_rate: int = 48000,
        channels: int = 2,
        bit_depth: int = 24,
    ) -> None:
        self._path = path
        self._sample_rate = int(sample_rate)
        self._bit_depth = int(bit_depth)
        self._ok = True
        self._container = None
        self._stream = None
        self._av = None
        self._layout = ""

        try:
            import av
        except ImportError:
            logger.error("PyAV not available, cannot write stem WAV")
            self._ok = False
            return
        self._av = av

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        codec = "pcm_s24le" if bit_depth == 24 else "pcm_s16le"
        # AM-08 阶段 4.1：用标准 ChannelLayout 推断 PyAV layout（替代 ad-hoc 字符串）。
        from src.features.edit.core.channel_layout import ChannelLayout
        try:
            layout = ChannelLayout.from_channel_count(channels).av_layout_name
        except ValueError:
            # 非标准声道数（如 4）回退到 Nc 形式
            layout = f"{channels}c"
        self._layout = layout

        try:
            self._container = av.open(path, mode="w", format="wav")
            stream = self._container.add_stream(codec, rate=int(sample_rate))
            stream.layout = layout
            self._stream = stream
        except Exception as e:  # noqa: BLE001 — 打开失败转 bool 结果
            logger.warning(f"Failed to open stem WAV {path}: {e}")
            self._ok = False

    @property
    def usable(self) -> bool:
        """写入器是否可用（容器/编码流已就绪）。"""
        return self._ok and self._container is not None

    def write(self, block: np.ndarray) -> None:
        """写入一块 PCM (channels, n) float32（自动 clip 到 [-1, 1]）。"""
        if not self.usable or block.size == 0:
            return
        if block.ndim == 1:
            block = block.reshape(1, -1)
        # fltp 格式输入（float planar），编码器负责量化
        clipped = np.clip(block, -1.0, 1.0).astype(np.float32)
        frame = self._av.AudioFrame.from_ndarray(
            clipped, format="fltp", layout=self._layout)
        frame.sample_rate = self._sample_rate
        for packet in self._stream.encode(frame):
            self._container.mux(packet)

    def close(self) -> bool:
        """flush 编码器并关闭容器；返回是否成功。"""
        if not self.usable:
            return False
        try:
            for packet in self._stream.encode(None):
                self._container.mux(packet)
            self._container.close()
            self._container = None
            logger.info(
                f"Stem WAV written: {self._path} "
                f"({self._bit_depth}bit, {self._sample_rate}Hz)")
            return True
        except Exception as e:  # noqa: BLE001 — 收尾失败转 bool 结果
            logger.warning(f"Failed to finalize stem WAV {self._path}: {e}")
            try:
                self._container.close()
            except Exception:  # noqa: BLE001 — 关闭失败无法进一步处理
                pass
            self._container = None
            return False


def write_stem_wav(
    path: str,
    samples: np.ndarray,
    sample_rate: int = 48000,
    bit_depth: int = 24,
) -> bool:
    """将 stem PCM 写入 WAV 文件（PCM s16le/s24le）。

    兼容入口：接受整段 (channels, n) 数组，内部经
    ``_StemWavWriter`` 分块写入（生产流式导出直接使用 writer）。

    Args:
        path: 输出路径（.wav）
        samples: (channels, n) PCM float32
        sample_rate: 采样率
        bit_depth: 16 或 24（决定编码器 PCM 位深）

    Returns:
        成功 True，失败 False
    """
    if samples.ndim == 1:
        samples = samples.reshape(1, -1)
    writer = _StemWavWriter(
        path, sample_rate=sample_rate, channels=int(samples.shape[0]),
        bit_depth=bit_depth)
    if not writer.usable:
        return False
    try:
        # 分块写入（避免一次性大内存）
        n = samples.shape[1]
        clipped = np.clip(samples, -1.0, 1.0).astype(np.float32)
        chunk = 8192
        for i in range(0, n, chunk):
            writer.write(clipped[:, i:i + chunk])
        return writer.close()
    except Exception as e:  # noqa: BLE001 — 写入失败转 bool 结果
        logger.warning(f"Failed to write stem WAV {path}: {e}")
        try:
            writer.close()
        except Exception:  # noqa: BLE001 — 收尾失败无法进一步处理
            pass
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 顶层：导出全部 stem
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StemExportResult:
    """Stem 导出结果。"""
    stems: List[Tuple[str, bool]] = field(default_factory=list)  # (name, success)
    output_dir: str = ""
    sample_rate: int = 48000
    channels: int = 2
    cancelled: bool = False
    # F3.4：路由 sidecar 路径（routing.json 与 stem WAV 同目录；含
    # Bus/Send 结构与重组声明——分轨重组按此 manifest 重建，而非简单相加）
    routing_manifest_path: str = ""

    @property
    def all_succeeded(self) -> bool:
        return all(s for _, s in self.stems)


def export_all_stems(
    sequence: "TimelineSequence",
    output_dir: str,
    decode_clip_fn: Optional[Callable] = None,
    sample_rate: int = 48000,
    channels: int = 2,
    bit_depth: int = 24,
    include_master: bool = True,
    group_stems: Optional[Tuple[str, ...]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    specs: Optional[List[StemSpec]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    sequence_resolver: Optional[Callable] = None,
    asset_lookup: Optional[Callable] = None,
    pre_render_base_fn: Optional[Callable] = None,
    frame_range: Optional[Tuple[int, int]] = None,
) -> StemExportResult:
    """导出工程的全部 stem（AM-10 阶段 3.5 顶层入口）。

    Args:
        sequence: TimelineSequence
        output_dir: 输出目录（stem 文件写入此目录）
        decode_clip_fn: clip 解码回调，None=静音（测试用）
        sample_rate: 采样率
        channels: 声道数
        bit_depth: 16/24
        include_master: stem 是否经 master stage
        group_stems: 显式 group 标签，None=自动检测
        progress_callback: (current, total, stem_name) 进度回调
        specs: 显式 stem 列表（B2 对话框按用户勾选传子集）；
            None=按 plan_stems_from_sequence 自动规划
        cancel_check: 每个 stem 渲染前轮询；返回 True 时中止（已完成的
            stem 保留，结果标记 cancelled）。B2 worker 取消用。
        sequence_resolver / asset_lookup: Snapshot lookups for strict nested
            audio expansion before planning and opening WAV writers.

    Returns:
        StemExportResult
    """
    # Expand once before planning or opening any WAV writer. Domain failures
    # propagate to the caller, rather than turning a missing nested stem into
    # an empty successful file/group.
    if asset_lookup is not None:
        from src.features.edit.core.nested_sequence import flatten_nested_audio_tracks

        sequence = replace(sequence, audio_tracks=flatten_nested_audio_tracks(
            sequence, sequence_resolver, asset_lookup, strict=True,
        ))
    if specs is None:
        specs = plan_stems_from_sequence(
            sequence, include_master=include_master, group_stems=group_stems,
        )
    # AUD-18217：显式传入的 specs 同样可能同名（如用户勾选两条同名轨），
    # 去重幂等，对已去重列表是无操作。
    specs = dedupe_stem_filenames(specs)
    result = StemExportResult(
        output_dir=output_dir, sample_rate=sample_rate, channels=channels,
    )

    total = len(specs)
    for i, spec in enumerate(specs):
        if cancel_check is not None and cancel_check():
            result.cancelled = True
            return result
        if progress_callback is not None:
            progress_callback(i, total, spec.name)
        path = os.path.join(output_dir, spec.filename)
        ok = True
        writer: Optional[_StemWavWriter] = None
        try:
            # AUD-18216：流式渲染逐块写入，不再整条时间线驻留内存。
            # 先探测首块：无内容的 stem 保持旧行为（不产出文件）。
            gen = _iter_stem_pcm_chunks(
                sequence, spec, sample_rate, channels, decode_clip_fn,
                pre_render_base_fn=pre_render_base_fn,
                frame_range=frame_range,
            )
            first = next(gen, None)
            if first is None:
                logger.info(f"Stem '{spec.name}' has no content, skipping")
                result.stems.append((spec.name, True))
                continue
            writer = _StemWavWriter(
                path, sample_rate=sample_rate, channels=channels,
                bit_depth=bit_depth)
            if not writer.usable:
                result.stems.append((spec.name, False))
                continue
            writer.write(first)
            first = None  # 尽早释放首块引用
            for chunk_pcm in gen:
                writer.write(chunk_pcm)
        except Exception as e:  # noqa: BLE001 — 单 stem 失败不阻断其余
            logger.warning(f"Stem '{spec.name}' export failed: {e}")
            ok = False
        finally:
            if writer is not None:
                try:
                    closed = writer.close()
                except Exception as e:  # noqa: BLE001 — 收尾异常计为失败
                    logger.warning(f"Stem '{spec.name}' close failed: {e}")
                    closed = False
            else:
                closed = True
        result.stems.append((spec.name, ok and closed))

    # F3.4：路由 sidecar——与 stem WAV 同目录（声明分轨重组的真实路由）
    try:
        import json
        manifest = build_routing_manifest(sequence)
        manifest_path = os.path.join(output_dir, "routing.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        result.routing_manifest_path = manifest_path
    except Exception as e:  # noqa: BLE001 — sidecar 失败不阻断 stems
        logger.warning(f"routing manifest write failed: {e}")

    if progress_callback is not None:
        progress_callback(total, total, "")

    return result


def build_routing_manifest(sequence: "TimelineSequence") -> dict:
    """F3.4 · 路由 sidecar——分轨重组的明确路由声明（纯函数）。

    内容：每条音频轨/bus 的 output_bus_id、sends（目标+电平+post-fader
    位置）、master 链与重组声明。存在 sends/Bus 路由时**简单相加 ≠ 混音**
    （post-fader tap 双计、MasterLimiter 非线性、成片响度归一只作用主混
    音）——重组必须按本 manifest 的路由图重建，这是对用户的显式声明，
    不再只散落在代码注释里。
    """
    tracks_out = []
    has_routing = False
    for t in getattr(sequence, "audio_tracks", ()) or ():
        sends = [
            {
                "target_bus_id": str(getattr(s, "target_bus_id", "")),
                "level": float(getattr(s, "level", 0.0)),
                "enabled": bool(getattr(s, "enabled", True)),
                "tap": "post-fader",
            }
            for s in (getattr(t, "sends", ()) or ())
        ]
        output_bus = getattr(t, "output_bus_id", None)
        if output_bus or sends:
            has_routing = True
        tracks_out.append({
            "track_id": t.track_id,
            "name": getattr(t, "name", "") or t.track_id,
            "kind": "bus" if getattr(t, "is_bus", False) else "track",
            "output_bus_id": output_bus,
            "sends": sends,
        })
    return {
        "schema": "topos.stem_routing/1",
        "sample_semantics": "48kHz reference; see sample_time.py",
        "has_routing": has_routing,
        "tracks": tracks_out,
        "master_chain": {
            "limiter": "continuous-state, threshold=1.0 (nonlinear)",
            "loudness_normalization": "final render only (not stems)",
        },
        "recombination": (
            "分轨重组按本 manifest 路由图重建（output_bus_id 汇聚 + sends "
            "post-fader tap）；存在 Bus/Send 路由时简单相加不等于混音。"
        ) if has_routing else (
            "无 Bus/Send 路由：分轨相加 ≈ 混音（MasterLimiter 未激活、"
            "无响度归一时逐位一致）。"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# B2: 生产解码工厂（对话框 worker 用）
# ─────────────────────────────────────────────────────────────────────────────

def create_pre_render_base_fn(
    project: Any,
    sequence: "TimelineSequence",
) -> Callable[[Any], Optional[np.ndarray]]:
    """§3.3：Stem 导出的预渲染基底工厂（按 clip_id 记忆化 + 同步构建）。

    P0：只有含预渲染步骤的 clip 才触发解析；非 READY（阻断/失败/取消）
    经 ``ensure_exportable`` 中止导出并上报片段、步骤和原因——Stem 不得
    以成功结果静默丢掉前缀里烘焙的效果。
    """
    memo: Dict[str, Optional[np.ndarray]] = {}

    def _base_for(clip: Any) -> Optional[np.ndarray]:
        clip_id = str(getattr(clip, "clip_id", "") or "")
        if clip_id in memo:
            return memo[clip_id]
        from src.features.edit.core.audio_edit import requires_pre_render
        audio_edit = getattr(clip, "audio_edit", None)
        if audio_edit is None or not requires_pre_render(audio_edit):
            memo[clip_id] = None
            return None
        from src.features.edit.core.pcm_render_cache import (
            resolve_export_pre_render_base,
        )
        asset = getattr(project, "assets", {}).get(
            getattr(clip, "asset_id", "")) if project is not None else None
        resolution = resolve_export_pre_render_base(
            project, sequence, clip, asset)
        resolution.ensure_exportable()
        memo[clip_id] = resolution.base
        return resolution.base

    return _base_for


def create_clip_decode_fn(
    project: Any,
    fps: float,
    sample_rate: int,
) -> Tuple[Callable, Callable[[], None]]:
    """构建生产用 decode_clip_fn（与时间线导出同源的取源/变速/声道逻辑）。

    与 timeline_export 的共享路径（第六轮审计 D01/D05 + 第七轮 R05/R06）：
    - retime（reverse/速度曲线/普通变速）走 ``audio_retime.ClipRetimeReader``
      ——canonical 帧映射 + 样本域重采样，与成片导出逐样本一致。
    - preserve-pitch（WSOLA）走共享 ``PreservedPitchReader``——旧 Stem
      路径明确回退变调，保调工程的成片与 Stem 听感不一致。
    - 冻结片段返回 None（无音频进度），与成片混音同语义。
    - 1x 片段经 ``timeline_to_source_frame`` canonical 读源（含混合
      fps/帧相位修正），解码定位用素材源帧率时钟。

    Args:
        project: EditProject（用于 export_source_path 代理策略取源）
        fps: 时间线帧率
        sample_rate: 输出采样率

    Returns:
        (decode_fn, close_fn)。decode_fn(track, clip, frame_idx, fps,
        n_samples) → (channels, n) float32 或 None；close_fn() 释放全部
        解码器（worker 结束时必须调用）。
    """
    from src.features.edit.core.audio_playback import AudioDecoder
    from src.features.edit.core.audio_retime import (
        ClipRetimeReader,
        PreservedPitchReader,
        clip_source_fps,
        needs_retime,
        preserve_pitch_request,
    )
    from src.features.edit.core.proxy_policy import export_source_path

    decoders: Dict[str, AudioDecoder] = {}
    retime_readers: Dict[str, Optional[ClipRetimeReader]] = {}
    pp_readers: Dict[str, Optional[PreservedPitchReader]] = {}
    _pp_unsupported_warned: set = set()
    # F3.3：子帧偏移门（时间线侧+源侧合计；与成片导出/实时预览同语义）
    from src.features.edit.core.sample_time import SampleSkipGate
    _sample_skip_gates: Dict[str, SampleSkipGate] = {}
    spf = int(sample_rate / fps) if fps > 0 else 0

    def decode_clip(
        track: Any,
        clip: Any,
        frame_idx: int,
        fps_value: float,
        n_samples: int,
    ) -> Optional[np.ndarray]:
        del track  # render_stem_pcm 按轨筛选后才回调，此处无需 track
        # D05：冻结段无音频进度——成片混音跳过，Stem 同语义返回 None。
        if getattr(clip, "freeze_source_frame", None) is not None:
            return None
        asset = getattr(project, "assets", {}).get(getattr(clip, "asset_id", "")) \
            if project is not None else None
        if asset is None:
            return None
        if getattr(asset, "is_nested_sequence", False):
            return None

        # F2.1 · Source Patching：素材级源声道映射（保调/retime/直读三条
        # 返回路径统一经此选道，与成片导出/预览同语义）。无映射时恒等。
        from src.features.edit.core.source_patch import patch_for_asset

        def _select_source_channels(
                samples: Optional[np.ndarray]) -> Optional[np.ndarray]:
            if samples is None:
                return None
            patch = patch_for_asset(
                getattr(project, "source_patch_overrides", None),
                clip.asset_id)
            return patch.select_channels(samples)

        def _apply_sample_gate(
                samples: Optional[np.ndarray]) -> Optional[np.ndarray]:
            """F3.3：子帧偏移门（成片/预览同刻；pp/retime/1x 三路统一）。"""
            if samples is None or samples.shape[-1] == 0:
                return samples
            gate = _sample_skip_gates.get(clip.clip_id)
            if gate is None:
                from src.features.edit.core.sample_time import (
                    SampleSkipGate as _Gate,
                )
                gate = _Gate(int(getattr(clip, "audio_sample_offset", 0) or 0)
                             + int(getattr(clip, "source_sample_offset", 0) or 0))
                _sample_skip_gates[clip.clip_id] = gate
            return gate.feed(samples)

        asset_path = export_source_path(project, asset)
        if not asset_path:
            return None

        # F2.10/M03（S05）：外录绑定——音频消费切到 WAV 源（偏移折算）；
        # retime/保调片段同样使用 WAV（reader 的 source_offset_seconds
        # 平移源窗，与成片/实时预览同口径——此前与成片同政策对变速
        # 绑定片段静默回退机内声）。仅 WAV 缺失/不可读才回退。
        from src.features.edit.core.audio_sync import (
            binding_for_asset,
            bound_audio_frame,
        )
        binding = binding_for_asset(
            getattr(project, "audio_sync_bindings", None) or {},
            clip.asset_id)
        audio_offset = 0.0
        if binding is not None:
            wav = project.assets.get(binding.wav_asset_id)
            wav_path = getattr(wav, "path", "") if wav is not None else ""
            if wav_path:
                asset_path = wav_path
                audio_offset = binding.offset_seconds
            else:
                logger.warning(
                    "stem clip %s: bound WAV asset unreadable (%s) "
                    "— falling back to camera audio",
                    clip.clip_id, binding.wav_asset_id)
                binding = None
        if binding is not None:
            decoder_key = (clip.asset_id, asset_path)
        else:
            decoder_key = clip.asset_id

        decoder = decoders.get(decoder_key)
        if decoder is None:
            decoder = AudioDecoder()
            if not decoder.open(
                asset_path,
                timeline_fps=float(fps_value),
                output_sample_rate=int(sample_rate),
            ):
                return None
            decoders[decoder_key] = decoder
        elif float(fps_value) != float(fps):
            # fps 漂移守卫：render_stem_pcm 全程使用同一 fps，正常不触发
            decoder.close()
            if not decoder.open(
                asset_path,
                timeline_fps=float(fps_value),
                output_sample_rate=int(sample_rate),
            ):
                return None

        # R05：保调变速走共享 WSOLA 读取器（与成片同一实现）。
        requested, pp_speed = preserve_pitch_request(clip)
        if requested:
            if pp_speed is None:
                if clip.clip_id not in _pp_unsupported_warned:
                    _pp_unsupported_warned.add(clip.clip_id)
                    logger.warning(
                        "stem clip %s preserve-pitch 不支持倒放/速度曲线，回退变调",
                        clip.clip_id,
                    )
            else:
                pp_reader = pp_readers.get(clip.clip_id, False)
                if pp_reader is False:
                    candidate = PreservedPitchReader(
                        decoder, clip, float(fps), spf, int(sample_rate),
                        source_offset_seconds=audio_offset)
                    pp_reader = candidate if candidate.available else None
                    pp_readers[clip.clip_id] = pp_reader
                    if pp_reader is None:
                        logger.warning(
                            "stem clip %s preserve-pitch 预解码失败，回退变调",
                            clip.clip_id,
                        )
                if pp_reader is not None:
                    return _apply_sample_gate(_select_source_channels(
                        pp_reader.read_frame(frame_idx, int(n_samples))))

        # D01：retime 片段走共享样本域重采样（与成片导出同源）。
        if needs_retime(clip):
            reader = retime_readers.get(clip.clip_id, False)
            if reader is False:
                reader = ClipRetimeReader(
                    decoder, clip, float(fps), spf,
                    sample_rate=int(sample_rate),
                    source_offset_seconds=audio_offset)
                retime_readers[clip.clip_id] = (
                    reader if reader.available else None
                )
                if not reader.available:
                    logger.warning(
                        "stem clip %s retime 窗口不可用，本帧静音跳过",
                        clip.clip_id,
                    )
                    reader = None
            if reader is not None:
                return _apply_sample_gate(_select_source_channels(
                    reader.read_frame(frame_idx, int(n_samples))))

        # 1x 直读：canonical 映射（混合 fps/帧相位由模型处理）。
        # R06：解码定位用素材源帧率时钟。
        try:
            source_frame = clip.timeline_to_source_frame(frame_idx)
        except (ValueError, AttributeError):
            return None
        if audio_offset:
            source_frame = bound_audio_frame(
                source_frame, audio_offset,
                clip_source_fps(clip, float(fps_value)))
        samples = decoder.decode_samples_for_frame(
            source_frame, clip_source_fps(clip, float(fps_value)),
            int(n_samples),
        )
        if samples is None:
            return None
        if samples.ndim == 1:
            samples = samples.reshape(1, -1)
        return _apply_sample_gate(_select_source_channels(
            np.ascontiguousarray(samples, dtype=np.float32)))

    def close_all() -> None:
        for decoder in decoders.values():
            try:
                decoder.close()
            except Exception:  # noqa: BLE001 — 释放失败不阻塞导出收尾
                pass
        decoders.clear()
        retime_readers.clear()
        pp_readers.clear()
        _pp_unsupported_warned.clear()

    return decode_clip, close_all
