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

    Yields:
        (channels, n) float32 PCM 块，顺序拼接即完整 stem。
    """
    from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
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
    if end_frame <= start_frame:
        return

    # 审计修复：Rational 精确换算（与 timeline_export 的 P1-15 基准一致），
    # 旧 float 秒换算在 29.97/长时长下与导出侧漂移
    total_samples = frames_to_samples(
        max(0, int(end_frame) - int(start_frame)), fps_exact, int(sample_rate))
    if total_samples <= 0:
        return

    # P1-02: 嵌套序列音频展开（可选；与实时/导出共用同一纯函数）。
    # 必须先于 stem_ids——MASTER 的"全部轨"要含虚拟轨。
    if asset_lookup is not None:
        from src.features.edit.core.nested_sequence import flatten_nested_audio_tracks

        expanded = flatten_nested_audio_tracks(
            sequence, sequence_resolver, asset_lookup, strict=True,
            frame_range=(start_frame, end_frame),
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
            overlap_start = max(clip_start, start_frame)
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
    # render_track_block(clip_fx_chain=...) 应用 clip FX，Stem 此前完全不
    # 传，Master stem 与成片 PCM 不一致。
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

    # carry: 块边界处帧块尾部超出块尾的样本 (绝对样本位, 数据)。
    carry: List[Tuple[int, np.ndarray]] = []

    for f0 in range(start_frame, end_frame, chunk_frames):
        f1 = min(f0 + chunk_frames, end_frame)
        s0 = frames_to_samples(f0 - start_frame, fps_exact, sample_rate)
        s1 = frames_to_samples(f1 - start_frame, fps_exact, sample_rate)
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

        for track, clip, ovl_start, ovl_end in work:
            frame_lo = max(ovl_start, f0)
            frame_hi = min(ovl_end, f1)
            for frame_idx in range(frame_lo, frame_hi):
                # D06：块长用精确相邻样本边界差（与成片导出同源）——
                # 固定 floor(sr/fps) 在非整数帧率下每帧留零样本洞。
                out_start = frames_to_samples(frame_idx - start_frame, fps_exact, sample_rate)
                out_next = frames_to_samples(
                    frame_idx + 1 - start_frame, fps_exact, sample_rate)
                block_len = max(0, out_next - out_start)
                if block_len <= 0:
                    continue
                # 解码（调用方提供真实解码，或测试用静音）
                if decode_clip_fn is not None:
                    samples = decode_clip_fn(track, clip, frame_idx, fps, block_len)
                else:
                    samples = np.zeros((channels, block_len), dtype=np.float32)

                if samples is None:
                    continue

                # P1-15: 精确有理数换算（fps 为 Rational 时无浮点漂移）。
                # 截断口径与整段渲染一致：以 total_samples 为上界。
                actual = min(samples.shape[-1], total_samples - out_start)
                if actual <= 0:
                    continue

                # P1-09：与时间线导出同源的声道适配——标准布局走 BS.775
                # downmix 矩阵（保留 Center/LFE/Surround 贡献），未知布局
                # 回退截断/复制并留警告日志（替代此前静默 naive 截断）。
                samples = adapt_samples_for_delivery(
                    samples[:, :actual] if samples.ndim == 2 else samples,
                    channels,
                    context=f"stem {getattr(track, 'stem_group', '')} clip {getattr(clip, 'clip_id', '')}",
                )

                # C3: 片段级增益（与 timeline_export/实时播放同口径——
                # 在 renderer 节点链之前乘线性增益；漏掉会使 Master stem
                # 与成片音轨/主混音不一致）。
                _clip_gain_db = float(getattr(clip, "audio_gain_db", 0.0) or 0.0)
                if abs(_clip_gain_db) > 1e-9:
                    samples = samples * (10.0 ** (_clip_gain_db / 20.0))

                clip_fade = {
                    "timeline_start": getattr(clip, "timeline_start", 0),
                    "timeline_duration": getattr(clip, "timeline_duration", 0),
                    "fade_in_frames": int(getattr(clip, "fade_in_duration", 0) or 0),
                    "fade_out_frames": int(getattr(clip, "fade_out_duration", 0) or 0),
                    "fps": fps,
                    # B16：淡化曲线（预览/成片/Stem 同键）；R09：两端独立
                    "fade_curve": getattr(clip, "fade_curve", None),
                    "fade_curve_in": getattr(
                        clip, "effective_fade_curve_in", None),
                    "fade_curve_out": getattr(
                        clip, "effective_fade_curve_out", None),
                }
                # D05：音频转场 crossfade 包络——与成片导出同一收集逻辑，
                # Stem 此前完全缺失。
                if getattr(track, "transitions", None):
                    from src.features.edit.core.audio_transition_catalog import (
                        audio_curve_for,
                    )
                    xfades = []
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
                        xfades.append({
                            "role": role,
                            "curve": curve,
                            "start_frame": tr.start_frame,
                            "duration": tr.duration,
                        })
                    if xfades:
                        clip_fade["crossfade"] = xfades
                processed = renderer.render_track_block(
                    track.track_id, samples, frame_idx,
                    clip_fx_chain=_clip_fx_for(clip, frame_idx),
                    clip_fade=clip_fade,
                    fps=fps,
                )
                if processed is None:
                    continue
                proc = processed[:channels, :actual]

                # 块内落位；超出块尾的部分留给下一块（carry）
                local = out_start - s0
                take = min(actual, chunk_len - local)
                if take > 0:
                    node = renderer.get_node(track.track_id)
                    target = node.output_bus_id if node else None
                    if target is not None and target in stem_ids:
                        if target not in bus_bufs:
                            bus_bufs[target] = np.zeros(
                                (channels, chunk_len), dtype=np.float32)
                        bus_bufs[target][:, local:local + take] += proc[:, :take]
                    elif stem.stem_type == StemType.BUS:
                        # F3.4 修复：BUS stem 的语义 = 该 Bus 的输出。
                        # 路由到别处/直达 Master 的轨只经下方 send 贡献，
                        # 干声不再落 stem 缓冲（旧实现把 send 源的干声
                        # 直接混进 BUS stem，与成片路由语义不符）。
                        pass
                    else:
                        buf[:, local:local + take] += proc[:, :take]
                    # F3.4 修复：sends 并行 tap 此前在 stem 路径整段丢失
                    # （成片经 renderer.route_track_output 处理，stem 落位
                    # 循环没有 send 分支）——任何带 send 的工程 Master
                    # stem ≠ 成片。此处与 route_track_output 同语义：
                    # post-fader 并行 tap × send_level，只认本 stem 集合
                    # 内的已注册 bus（集合外的 send 落空，由 stem 语义
                    # 决定——该 bus 不在本 stem 中）。
                    if node is not None:
                        for send_bus_id, send_level in node.sends:
                            if send_level <= 0.0 or send_bus_id not in stem_ids:
                                continue
                            if send_bus_id not in bus_bufs:
                                bus_bufs[send_bus_id] = np.zeros(
                                    (channels, chunk_len), dtype=np.float32)
                            bus_bufs[send_bus_id][
                                :, local:local + take
                            ] += proc[:, :take] * send_level
                if take < actual and f1 < end_frame:
                    carry.append((out_start + take, proc[:, take:]))

        # Bus stage（仅本块的 Bus 输入；renderer 自动补零缓冲）
        # RMA-04: 传入可听闭包；RMA-06: 传入块起始帧/fps 让 Bus automation 求值
        renderer.render_bus_stage(bus_bufs, buf, has_solo=has_solo,
                                  audible_solo=audible_solo,
                                  start_frame=f0, fps=fps)

        # Master stage（可选；连续状态 limiter 跨块一致）
        # §3.1: 传入块起始帧/fps 供 master automation ramp
        if stem.include_master:
            buf = renderer.apply_master(buf, start_frame=f0, fps=fps)

        yield buf


def render_stem_pcm(
    sequence: "TimelineSequence",
    stem: StemSpec,
    sample_rate: int = 48000,
    channels: int = 2,
    decode_clip_fn: Optional[Callable] = None,
    sequence_resolver: Optional[Callable] = None,
    asset_lookup: Optional[Callable] = None,
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
