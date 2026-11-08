"""
Audio Graph Renderer — 实时/导出共享节点处理（AM-03 阶段 2）。

本模块是计划表 §5.1 "唯一音频图" 的核心实现：把音频混音的节点处理逻辑
抽取为单一来源，让实时播放（AudioMixer.mix_samples）与离线导出
（TimelineExporter）调用**同一份节点顺序与 DSP**，消除两路径漂移。

设计动机（AM-03）
-----------------
此前导出走独立的逐帧简化混音（仅 volume/pan + 硬削波），完全缺失 clip FX、
track EQ/FX、automation、Bus/Send 路由、master volume/mute、软限幅，导致
预览听到的处理在导出文件中丢失。

本 renderer 从 `TimelineSequence`/`EditTrack`/`ClipInstance` 模型构建节点图，
节点顺序与 `AudioMixer.mix_samples` 严格一致：

    普通轨：clip fade → automation(volume/pan/mute 求值) → clip FX → volume → pan → strip EQ → track FX
            → 路由(output_bus_id/sends → bus_inputs 或 master)
    Bus 轨：拓扑序(volume/pan/EQ/FX) → 路由(下游 bus 或 master)
    Master：volume 缩放 + mute + 软限幅(比例缩放)

线程安全
--------
本类**非线程安全**，专为单线程使用设计：
- 实时路径仍由 AudioMixer（持 QMutex）驱动，本 renderer 不直接用于实时
  （实时路径保持现有 mix_samples 不变，零回归风险）。
- 导出路径在导出线程单线程调用。

本 renderer 的价值在于：导出路径复用与实时**语义相同**的节点处理，由
parity 测试锁定两者输出一致。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np

from src.features.edit.core.audio_dsp import EffectChain, StripEQFilter
from src.features.edit.core.audio_playback import topological_sort_buses

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# automation 投影：深拷贝关键帧数据（AUD-15400）
# ─────────────────────────────────────────────────────────────────────────────

def _snapshot_keyframe_track(track: Any) -> Any:
    """把模型 KeyframeTrack 值拷贝为独立实例（AUD-15400）。

    节点此前用 ``dict(automation)`` 浅拷贝——只隔离字典结构，KeyframeTrack
    对象仍与 UI 模型共享：导出线程经 ``get_value_ramp → get_value_float``
    遍历 ``keyframes`` 列表时，UI 线程增删关键帧会引发 IndexError / 撕裂值，
    与 audio_graph_snapshot 自述的"禁止播放/导出线程读取 UI 对象"原则冲突。
    投影时一次性值拷贝（frame/value/插值参数全部独立），之后导出线程只读
    私有副本。拷贝瞬间对 keyframes 取 list() 浅快照，迭代本身不被并发修改
    击溃（最坏情况错过最新一帧，属投影时点的正常语义）。
    非 KeyframeTrack 的 duck-typed 轨道保持原样（不改变现有行为）。
    """
    from src.shared.keyframe import Keyframe, KeyframeTrack
    if not isinstance(track, KeyframeTrack):
        return track
    return KeyframeTrack(
        param_name=track.param_name,
        keyframes=[
            Keyframe(
                frame=kf.frame,
                value=kf.value,
                interpolation=kf.interpolation,
                bezier_in=tuple(kf.bezier_in),
                bezier_out=tuple(kf.bezier_out),
                tangent_mode=kf.tangent_mode,
                handle_left=tuple(kf.handle_left) if kf.handle_left is not None else None,
                handle_right=tuple(kf.handle_right) if kf.handle_right is not None else None,
            )
            for kf in list(track.keyframes or ())
        ],
        min_val=track.min_val,
        max_val=track.max_val,
        pre_extrap=track.pre_extrap,
        post_extrap=track.post_extrap,
    )


def _snapshot_automation(automation: Any) -> dict:
    """{param: KeyframeTrack} 的深拷贝投影（AUD-15400）。"""
    return {
        param: _snapshot_keyframe_track(track)
        for param, track in dict(automation or {}).items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# 节点配置：从 EditTrack 投影的不可变描述
# ─────────────────────────────────────────────────────────────────────────────

class _TrackNode:
    """单个 track/bus 的节点配置（从 EditTrack 投影）。

    持有该节点的静态参数（volume/pan/muted/solo/enabled/is_bus/output_bus_id/sends）
    与 DSP 处理器实例（EQ/FX，有状态 IIR）。automation 作为动态参数在 render 时求值。
    """

    __slots__ = (
        "track_id", "is_bus", "volume", "pan", "muted", "solo", "enabled",
        "output_bus_id", "sends", "automation", "automation_mode",
        "eq_filter", "effect_chain",
    )

    def __init__(self, track: Any, sample_rate: float) -> None:
        self.track_id = getattr(track, "track_id", "")
        self.is_bus = bool(getattr(track, "is_bus", False))
        self.volume = float(getattr(track, "volume", 1.0))
        self.pan = float(getattr(track, "pan", 0.0))
        self.muted = bool(getattr(track, "is_muted", False))
        self.solo = bool(getattr(track, "is_solo", False))
        self.enabled = bool(getattr(track, "is_enabled", True))
        # AM-07: 自动化模式（off/read/write/touch/latch）
        self.automation_mode = str(getattr(track, "automation_mode", "off") or "off").lower()
        self.output_bus_id = getattr(track, "output_bus_id", None)
        # sends: [(target_bus_id, level), ...] 仅 enabled 的
        raw_sends = getattr(track, "sends", ()) or ()
        self.sends = [
            (getattr(s, "target_bus_id", ""), float(getattr(s, "level", 0.0)))
            for s in raw_sends if getattr(s, "enabled", True)
        ]
        # AUD-15400: 深拷贝关键帧数据，切断节点与 UI 模型 KeyframeTrack 的活引用
        # （浅拷贝只隔离 dict 结构，对象仍共享，导出线程与 UI 编辑并发读写竞态）。
        self.automation = _snapshot_automation(getattr(track, "automation", {}))

        # DSP 处理器（有状态，按 track 独立）
        self.eq_filter = StripEQFilter(sample_rate)
        strip_eq = getattr(track, "strip_eq", None)
        if strip_eq is not None:
            self.eq_filter.set_params(strip_eq, sample_rate=sample_rate)

        self.effect_chain = EffectChain(sample_rate)
        chain = getattr(track, "effect_chain", None)
        if chain is not None and hasattr(chain, "to_dsp_entries"):
            self.effect_chain.set_params(chain.to_dsp_entries(), sample_rate=sample_rate)


# ─────────────────────────────────────────────────────────────────────────────
# AudioGraphRenderer：有状态块渲染器
# ─────────────────────────────────────────────────────────────────────────────

class AudioGraphRenderer:
    """从 TimelineSequence 构建的音频图块渲染器（AM-03 共享核心）。

    用法（导出路径）：
        renderer = AudioGraphRenderer(sample_rate)
        renderer.configure_from_sequence(sequence)
        for each frame:
            samples = decoder.decode_samples_for_frame(...)
            processed = renderer.render_track_block(track_id, samples, frame, fps)
            # 路由到 bus_inputs 或 master
        master += renderer.render_bus_stage(bus_inputs, has_solo, ...)
        output = renderer.apply_master(output, master_volume, master_muted)

    节点顺序与 AudioMixer.mix_samples 严格一致，由 parity 测试锁定。
    """

    def __init__(self, sample_rate: float = 48000.0) -> None:
        self._sample_rate = float(sample_rate)
        self._nodes: dict[str, _TrackNode] = {}
        self._master_volume: float = 1.0
        self._master_muted: bool = False
        # §3.1: master automation（{param: KeyframeTrack}）+ 模式
        self._master_automation: dict = {}
        self._master_automation_mode: str = "off"
        # AM-09: 连续状态 Master 限幅器（与 mix_samples 一致）
        from src.features.edit.core.audio_dsp import MasterLimiter
        self._master_limiter = MasterLimiter(self._sample_rate, threshold=1.0)

    @staticmethod
    def _apply_declick_edges(gate: np.ndarray, sample_rate: float,
                             declick_ms: float = 1.0) -> np.ndarray:
        """在 mute gate 的 0↔1 边界施加短线性 ramp，避免阶跃 click（RMA-07）。

        Args:
            gate: 1D 门控数组（1.0=有声，0.0=静音）。
            sample_rate: 采样率。
            declick_ms: 边界 ramp 时长（ms，默认 1ms，计划要求 1-5ms）。

        Returns:
            平滑后的 gate（边界处 1ms 线性渐入/渐出，而非瞬间跳变）。
        """
        n = gate.shape[-1]
        ramp_len = max(1, int(sample_rate * declick_ms / 1000.0))
        if n <= 1 or ramp_len >= n:
            return gate.astype(np.float32, copy=True)
        out = gate.astype(np.float32, copy=True)
        # 找跳变点：gate[i-1] != gate[i]，跳变发生在 i 处
        diffs = np.abs(np.diff(gate)) > 1e-6
        change_idx = np.nonzero(diffs)[0] + 1
        for idx in change_idx:
            if gate[idx] > 0.5:
                # 0→1（静音→有声）：渐入。有声段从 idx 开始，在其头部 ramp_len 内从 0 升到 1。
                e = min(n, idx + ramp_len)
                seg_len = e - idx
                if seg_len > 0:
                    ramp = np.linspace(0.0, 1.0, seg_len + 1, dtype=np.float32)[1:]
                    out[idx:e] = gate[idx:e] * ramp
            else:
                # 1→0（有声→静音）：渐出。有声段在 idx 之前结束，在其尾部 ramp_len 内从 1 降到 0。
                s = max(0, idx - ramp_len)
                seg_len = idx - s
                if seg_len > 0:
                    ramp = np.linspace(1.0, 0.0, seg_len + 1, dtype=np.float32)[:-1]
                    out[s:idx] = gate[s:idx] * ramp
        return out

    def configure_from_sequence(self, sequence: Any) -> None:
        """从 TimelineSequence 构建节点图（普通轨 + Bus 轨 + master）。"""
        audio_tracks = getattr(sequence, "audio_tracks", ()) or ()
        for track in audio_tracks:
            node = _TrackNode(track, self._sample_rate)
            self._nodes[node.track_id] = node
        self._master_volume = float(getattr(sequence, "master_volume", 1.0))
        self._master_muted = bool(getattr(sequence, "master_muted", False))
        # §3.1: master automation（AUD-15400: 同样深拷贝，切断活引用）
        self._master_automation = _snapshot_automation(
            getattr(sequence, "master_automation", {}) or {}
        )
        self._master_automation_mode = str(
            getattr(sequence, "master_automation_mode", "off") or "off"
        ).lower()

    def _build_routing_dict(self) -> dict:
        """从 self._nodes 构建 routing dict（供闭包/拓扑排序复用）。"""
        return {
            nid: {
                "output_bus_id": node.output_bus_id,
                "sends": list(node.sends),
                "is_bus": node.is_bus,
            }
            for nid, node in self._nodes.items()
        }

    def compute_audible_solo_closure(self) -> set:
        """RMA-04: 基于当前节点图计算 solo 可听闭包（实时/导出共用）。

        无 solo 时返回空集。调用方据此判断节点是否可听：
        `has_solo and track_id not in audible → 跳过`。
        """
        from src.features.edit.core.audio_playback import compute_audible_solo_closure
        solo_ids = [nid for nid, n in self._nodes.items() if n.solo]
        if not solo_ids:
            return set()
        return compute_audible_solo_closure(
            list(self._nodes.keys()), self._build_routing_dict(), solo_ids,
        )

    def is_track_audible(self, track_id: str, has_solo: bool, audible: set = None) -> bool:
        """RMA-04: 判断普通轨在 solo 语境下是否可听（替代 is_track_renderable 的 solo 分支）。

        Args:
            audible: 预计算的闭包（避免每轨重复算）；None 时内部计算。
        """
        if not has_solo:
            return True
        if audible is None:
            audible = self.compute_audible_solo_closure()
        return track_id in audible

    @property
    def master_volume(self) -> float:
        return self._master_volume

    @property
    def master_muted(self) -> bool:
        return self._master_muted

    def get_node(self, track_id: str) -> Optional[_TrackNode]:
        return self._nodes.get(track_id)

    # ── 普通轨节点处理（与 mix_samples 阶段 1 节点顺序一致）──

    def render_track_block(
        self,
        track_id: str,
        samples: np.ndarray,
        current_frame: int,
        clip_fx_chain: Optional[EffectChain] = None,
        clip_fade: Optional[dict] = None,
        source_channels: int = 2,
        fps: Optional[float] = None,
    ) -> Optional[np.ndarray]:
        """处理单个普通轨的一个样本块。

        节点顺序（与 AudioMixer.mix_samples 阶段 1 一致）：
        clip fade → automation(mute gate) → clip FX → automation(volume/pan)
        → volume → pan → strip EQ → track FX

        Args:
            track_id: 轨道 ID
            samples: 解码后的样本 (channels, n)
            current_frame: 当前时间线帧（automation 求值用）
            clip_fx_chain: 该 clip 的 FX 链（可选，pre-fader）
            clip_fade: clip 淡化参数 dict，含 keys:
                timeline_start, timeline_duration, fade_in_frames, fade_out_frames, fps。
                提供 AM-06 clip 淡入淡出（在所有其他节点之前）。
            source_channels: RMA-08 源素材原始声道数（1=mono→equal-power pan，
                2=stereo→balance unity-center，>2=surround→trim）。默认 2。
            fps: 时间线帧率（P1-20：automation ramp 的帧→样本换算基准）。
                显式传入优先；未传时回退 clip_fade["fps"]，再退 24.0。

        Returns:
            处理后的样本；若整块被 mute automation 静音则返回 None。
        """
        # AM-06: clip 淡化（source decode 之后、其他节点之前）
        if clip_fade is not None:
            from src.features.edit.core.audio_dsp import apply_clip_fade
            samples = apply_clip_fade(
                samples,
                clip_timeline_start=int(clip_fade.get("timeline_start", 0)),
                clip_timeline_duration=int(clip_fade.get("timeline_duration", 0)),
                current_frame=current_frame,
                fade_in_frames=int(clip_fade.get("fade_in_frames", 0)),
                fade_out_frames=int(clip_fade.get("fade_out_frames", 0)),
                fps=float(clip_fade.get("fps", 24.0)),
                sample_rate=self._sample_rate,
                fade_in_samples_override=clip_fade.get("fade_in_samples"),
                fade_out_samples_override=clip_fade.get("fade_out_samples"),
                audio_sample_offset=int(
                    clip_fade.get("audio_sample_offset", 0) or 0),
                fade_curve=clip_fade.get("fade_curve"),
                fade_curve_in=clip_fade.get("fade_curve_in"),
                fade_curve_out=clip_fade.get("fade_curve_out"),
            )
            # AT: 音频转场 crossfade（与 clip fade 同级，source 电平调整）。
            # crossfade 是一个 list，每个元素描述该 clip 参与的一个转场。
            for xf in clip_fade.get("crossfade", ()) or ():
                from src.features.edit.core.audio_dsp import apply_crossfade_block
                samples = apply_crossfade_block(
                    samples,
                    transition_start_frame=int(xf.get("start_frame", 0)),
                    transition_duration_frames=int(xf.get("duration", 0)),
                    current_frame=current_frame,
                    role=str(xf.get("role", "")),
                    curve=str(xf.get("curve", "constant_power")),
                    fps=float(clip_fade.get("fps", 24.0)),
                    sample_rate=self._sample_rate,
                )
        node = self._nodes.get(track_id)
        if node is None:
            return samples

        # 静态 mute/solo 由调用方在路由层处理（与 mix_samples 一致）；
        # 此处仅处理 automation 覆盖。
        volume = node.volume
        pan = node.pan

        # AM-07/§3.2: automation 模式判断 + 逐样本 ramp。
        # Off 模式不读取曲线。§3.2: 逐样本 ramp 避免阶跃 click。
        auto = node.automation if node.automation_mode != "off" else None
        volume_ramp = None
        pan_ramp = None
        mute_gate = None
        # P1-20: automation ramp 的帧→样本换算必须用真实时间线帧率。
        # 优先级：显式 fps 参数 > clip_fade["fps"] > 24.0 兜底。此前无
        # clip_fade 的调用会静默按 24fps 求值（29.97 时间线下 ramp 拉长）。
        fps_effective = 24.0
        if clip_fade is not None:
            fps_effective = float(clip_fade.get("fps", 24.0))
        if fps is not None:
            fps_effective = float(fps)
        if auto:
            # RMA-07: 逐样本 mute gate + de-click（与实时/Bus/Master 一致）。
            mute_track = auto.get("mute")
            if mute_track is not None and getattr(mute_track, "keyframes", None):
                mute_ramp = np.array(
                    mute_track.get_value_ramp(
                        float(current_frame), fps_effective, samples.shape[-1],
                        self._sample_rate, 0.0,
                    ),
                    dtype=samples.dtype,
                )
                mute_gate = (mute_ramp <= 0.5).astype(samples.dtype)
                mute_gate = self._apply_declick_edges(mute_gate, self._sample_rate)
                if not np.any(mute_gate):
                    return None
            vol_track = auto.get("volume")
            if vol_track is not None and getattr(vol_track, "keyframes", None):
                volume_ramp = np.array(
                    vol_track.get_value_ramp(
                        float(current_frame), fps_effective, samples.shape[-1],
                        self._sample_rate, volume,
                    ),
                    dtype=samples.dtype,
                )
            pan_track = auto.get("pan")
            if pan_track is not None and getattr(pan_track, "keyframes", None):
                pan_ramp = np.array(
                    pan_track.get_value_ramp(
                        float(current_frame), fps_effective, samples.shape[-1],
                        self._sample_rate, pan,
                    ),
                    dtype=samples.dtype,
                )

        # clip FX（pre-fader，在 volume 之前）
        if clip_fx_chain is not None and clip_fx_chain.is_active:
            samples = clip_fx_chain.process(samples)

        # volume（§3.2: ramp 时逐样本）
        if volume_ramp is not None:
            samples = samples * volume_ramp[np.newaxis, :]
        else:
            samples = samples * volume

        # pan（RMA-08: 按源声道数选算法——mono=equal-power，stereo=balance unity-center；
        # §3.2: ramp 时逐样本）
        from src.features.edit.core.audio_dsp import pan_gains_for_channels
        samples = samples.copy()
        if pan_ramp is not None:
            # 逐样本：mono 用 equal-power（cos/sin），stereo/surround 用 balance（线性衰减远侧）
            if source_channels <= 1:
                theta = (pan_ramp + 1.0) * 0.5 * (np.pi / 2.0)
                left_gains = np.cos(theta).astype(samples.dtype)
                right_gains = np.sin(theta).astype(samples.dtype)
            else:
                pr = np.clip(pan_ramp, -1.0, 1.0)
                left_gains = (1.0 - np.maximum(0.0, pr)).astype(samples.dtype)
                right_gains = (1.0 + np.minimum(0.0, pr)).astype(samples.dtype)
            samples[0] *= left_gains
            if samples.shape[0] > 1:
                samples[1] *= right_gains
        else:
            left_gain, right_gain = pan_gains_for_channels(pan, source_channels)
            samples[0] *= left_gain
            if samples.shape[0] > 1:
                samples[1] *= right_gain

        # strip EQ（post-fader）
        if node.eq_filter is not None and node.eq_filter.is_active:
            samples = node.eq_filter.process(samples)

        # track FX（post-EQ）
        if node.effect_chain is not None and node.effect_chain.is_active:
            samples = node.effect_chain.process(samples)

        # 与实时路径保持一致：mute gate 位于 strip 输出端，静音时不泄漏 FX 尾音。
        if mute_gate is not None:
            samples = samples * mute_gate[np.newaxis, :samples.shape[-1]]

        return samples

    # ── 路由：把处理后样本累积到 bus_inputs 或 master ──

    def _is_known_bus(self, bus_id: Optional[str]) -> bool:
        """AUD-15402: bus_id 是否为节点图中已注册的 Bus 节点。"""
        if not bus_id:
            return False
        node = self._nodes.get(bus_id)
        return node is not None and node.is_bus

    def route_track_output(
        self,
        track_id: str,
        samples: np.ndarray,
        output: np.ndarray,
        bus_inputs: dict,
    ) -> None:
        """把普通轨处理后的样本路由到 bus_inputs 或 master（与 mix_samples:1652-1668 一致）。

        output_bus_id 非 None → 累积到 bus_inputs[output_bus_id]
        并行处理 sends → bus_inputs[send_bus_id] *= level
        """
        node = self._nodes.get(track_id)
        target = node.output_bus_id if node else None
        n = samples.shape[-1]
        if target is not None:
            if self._is_known_bus(target):
                if target not in bus_inputs:
                    bus_inputs[target] = np.zeros_like(output)
                bus_inputs[target][:, :n] += samples
            else:
                # AUD-15402: 未知 bus 目标（如 bus 已删除但轨的路由未清理）。
                # 旧代码为未知目标也创建缓冲——该轨样本累积进永不被消费的
                # 孤儿缓冲，整轨音频在导出中静默丢失。与 Bus 阶段（render_bus_stage
                # 的路由兜底分支）语义统一：兜底进 master 并告警。
                logger.warning(
                    "AudioGraphRenderer: track %r routes to unknown bus %r; "
                    "falling back to master", track_id, target,
                )
                output[:, :n] += samples
        else:
            output[:, :n] += samples
        # sends（并行）
        if node is not None:
            for send_bus_id, send_level in node.sends:
                if send_level <= 0.0:
                    continue
                if not self._is_known_bus(send_bus_id):
                    # AUD-15402: send 指向未注册 bus——并行 tap 落空，跳过并
                    # 告警（与 Bus 阶段 send 只认已注册目标的语义一致）。
                    logger.warning(
                        "AudioGraphRenderer: track %r send targets unknown bus %r; "
                        "send skipped", track_id, send_bus_id,
                    )
                    continue
                if send_bus_id not in bus_inputs:
                    bus_inputs[send_bus_id] = np.zeros_like(output)
                bus_inputs[send_bus_id][:, :n] += samples * send_level

    # ── Bus 阶段（拓扑序，与 mix_samples 阶段 2 一致）──

    def render_bus_stage(
        self,
        bus_inputs: dict,
        output: np.ndarray,
        has_solo: bool,
        audible_solo: set = None,
        start_frame: int = 0,
        fps: float = 24.0,
    ) -> None:
        """按拓扑序处理 Bus，累积到 output（与 mix_samples 阶段 2 一致）。

        修改 output in-place（Bus 处理后路由到下游 bus 或 master）。

        Args:
            audible_solo: RMA-04 预计算的 solo 可听闭包（None 时内部计算）。
                Bus 必须在闭包内才保留，替代旧的 `not node.solo` 单点判断。
            start_frame/fps: RMA-06 Bus automation（volume/pan/mute）逐样本求值用。
                Bus automation 此前完全被忽略（UI/模型可保存却无声跳过）。
        """
        # RMA-04: 计算可听闭包（含普通轨 + Bus）。旧 `not node.solo` 会把 solo
        # 普通轨必经的 Bus 错误剔除。
        if has_solo and audible_solo is None:
            audible_solo = self.compute_audible_solo_closure()

        # 收集所有注册的 Bus 节点
        bus_nodes = [n for n in self._nodes.values() if n.is_bus]
        bus_ids = [n.track_id for n in bus_nodes]
        # 构建 routing dict（复用 topological_sort_buses，含 sends 供闭包/检测）
        routing = {
            nid: {
                "output_bus_id": self._nodes[nid].output_bus_id,
                "sends": list(self._nodes[nid].sends),
                "is_bus": True,
            }
            for nid in bus_ids
        }
        sorted_buses, cycle_buses = topological_sort_buses(bus_ids, routing)

        # AUD-15401: 环中的 Bus 被 topological_sort_buses 排除且不参与渲染，
        # 其自身及上游贡献的音频无声丢失。被调用方契约（audio_playback
        # topological_sort_buses docstring）要求调用方记录日志——此前
        # cycle_buses 被静默丢弃。与实时路径 mix_samples 的环告警行为对齐。
        if cycle_buses:
            logger.warning(
                "AudioGraphRenderer: %d bus(es) excluded from bus stage due to "
                "routing cycle: %s — audio routed through them is dropped",
                len(cycle_buses), sorted(cycle_buses),
            )

        # 确保每个 Bus 都有 buffer
        for bid in bus_ids:
            if bid not in bus_inputs:
                bus_inputs[bid] = np.zeros_like(output)


        for bus_id in sorted_buses:
            bus_input = bus_inputs.get(bus_id)
            if bus_input is None:
                continue
            node = self._nodes.get(bus_id)
            if node is None or not node.enabled or node.muted:
                continue
            # RMA-04: 用闭包判断，替代 `not node.solo`
            if has_solo and bus_id not in audible_solo:
                continue

            # RMA-06: Bus automation（volume/pan/mute）逐样本求值。
            # 此前 Bus 阶段只读静态 node.volume/node.pan，automation 被无声忽略。
            bus_volume = node.volume
            bus_pan = node.pan
            auto = node.automation if node.automation_mode != "off" else None
            vol_ramp = None
            pan_ramp = None
            if auto:
                vol_track = auto.get("volume")
                if vol_track is not None and getattr(vol_track, "keyframes", None):
                    vol_ramp = np.array(
                        vol_track.get_value_ramp(
                            float(start_frame), fps, output.shape[-1],
                            self._sample_rate, bus_volume,
                        ),
                        dtype=output.dtype,
                    )
                pan_track = auto.get("pan")
                if pan_track is not None and getattr(pan_track, "keyframes", None):
                    pan_ramp = np.array(
                        pan_track.get_value_ramp(
                            float(start_frame), fps, output.shape[-1],
                            self._sample_rate, bus_pan,
                        ),
                        dtype=output.dtype,
                    )
                mute_track = auto.get("mute")
                if mute_track is not None and getattr(mute_track, "keyframes", None):
                    # RMA-07: 逐样本 mute 门控 + de-click（与 master 一致）
                    mute_ramp = np.array(
                        mute_track.get_value_ramp(
                            float(start_frame), fps, output.shape[-1],
                            self._sample_rate, 0.0,
                        ),
                        dtype=output.dtype,
                    )
                    gate = (mute_ramp <= 0.5).astype(output.dtype)
                    gate = self._apply_declick_edges(gate, self._sample_rate)
                    bus_input = bus_input * gate[np.newaxis, :]

            # volume（automation ramp 或静态）
            if vol_ramp is not None:
                bus_samples = bus_input * vol_ramp[np.newaxis, :]
            else:
                bus_samples = bus_input * bus_volume
            # pan（RMA-08: Bus 输入是已混立体声，用 balance unity-center）
            from src.features.edit.core.audio_dsp import balance_gains
            if pan_ramp is not None and bus_samples.shape[0] >= 2:
                bus_samples = bus_samples.copy()
                for i in range(min(len(pan_ramp), bus_samples.shape[-1])):
                    lg, rg = balance_gains(float(pan_ramp[i]))
                    bus_samples[0, i] *= lg
                    bus_samples[1, i] *= rg
            else:
                lg, rg = balance_gains(bus_pan)
                bus_samples = bus_samples.copy()
                bus_samples[0] *= lg
                if bus_samples.shape[0] > 1:
                    bus_samples[1] *= rg
            # EQ
            if node.eq_filter is not None and node.eq_filter.is_active:
                bus_samples = node.eq_filter.process(bus_samples)
            # FX
            if node.effect_chain is not None and node.effect_chain.is_active:
                bus_samples = node.effect_chain.process(bus_samples)
            # 路由：Bus→Bus 或 →Master（output_bus_id）
            target = node.output_bus_id
            n = bus_samples.shape[-1]
            if target is not None and target in bus_inputs:
                bus_inputs[target][:, :n] += bus_samples
            else:
                if target is not None and not self._is_known_bus(target):
                    # AUD-15402: Bus 输出指向未注册 bus——兜底进 master 并告警
                    logger.warning(
                        "AudioGraphRenderer: bus %r routes to unknown bus %r; "
                        "falling back to master", bus_id, target,
                    )
                output[:, :n] += bus_samples
            # RMA-06: Bus sends（Bus 也可 send 到其他 Bus，并行 tap）。
            # 此前 Bus 阶段只处理 output_bus_id，sends 被无声忽略。
            for send_bus_id, send_level in node.sends:
                if send_level <= 0.0:
                    continue
                if send_bus_id in bus_inputs:
                    bus_inputs[send_bus_id][:, :n] += bus_samples * send_level
                elif not self._is_known_bus(send_bus_id):
                    # AUD-15402: Bus send 指向未注册 bus——并行 tap 落空，告警
                    logger.warning(
                        "AudioGraphRenderer: bus %r send targets unknown bus %r; "
                        "send skipped", bus_id, send_bus_id,
                    )

    # ── Master 阶段（与 mix_samples 阶段 3 一致）──

    def apply_master(self, output: np.ndarray, skip_volume: bool = False,
                     start_frame: int = 0, fps: float = 24.0) -> np.ndarray:
        """应用 master volume/mute + 连续状态限幅（AM-09，与 mix_samples 一致）。

        AM-09: 用连续状态 MasterLimiter 替代无状态峰值归一化，
        跨 buffer 平滑增益包络，避免 block-boundary gain jump（pumping）。

        §3.1: master automation（volume ramp + mute 判定），需 start_frame/fps。

        Args:
            skip_volume: 跳过 master volume/mute/automation，仅应用限幅。
                用于 AM-10 阶段 3.4 normalize 二次过 limiter（避免重复施加 master 增益）。
            start_frame: output 起始帧（master automation ramp 用）
            fps: 帧率（master automation ramp 用）
        """
        if not skip_volume:
            # §3.1: master automation
            auto_mode = self._master_automation_mode
            master_auto = self._master_automation if auto_mode != "off" else None
            # RMA-07: master mute automation 此前只读 start_frame（整段 output 单值），
            # 导出中途的 mute keyframe 被整体忽略。改为逐样本 ramp：生成 mute 门控
            # envelope，>0.5 处静音，并在静音边界做短 de-click ramp 避免阶跃 click。
            # 静态 master_muted 仍直接静音整段。
            static_muted = self._master_muted
            if static_muted:
                output = output * 0.0
            else:
                # mute automation 逐样本门控
                if master_auto:
                    mute_track = master_auto.get("mute")
                    if mute_track is not None and getattr(mute_track, "keyframes", None):
                        mute_ramp = np.array(
                            mute_track.get_value_ramp(
                                float(start_frame), fps, output.shape[-1],
                                self._sample_rate, 0.0,
                            ),
                            dtype=output.dtype,
                        )
                        # >0.5 静音；边界 de-click（1ms 线性 ramp，避免阶跃 click）
                        gate = (mute_ramp <= 0.5).astype(output.dtype)
                        gate = self._apply_declick_edges(gate, self._sample_rate)
                        output = output * gate[np.newaxis, :]
                # volume automation 逐样本 ramp（原已正确）
                vol_ramp = None
                if master_auto:
                    vol_track = master_auto.get("volume")
                    if vol_track is not None and getattr(vol_track, "keyframes", None):
                        vol_ramp = np.array(
                            vol_track.get_value_ramp(
                                float(start_frame), fps, output.shape[-1],
                                self._sample_rate, self._master_volume,
                            ),
                            dtype=output.dtype,
                        )
                if vol_ramp is not None:
                    output = output * vol_ramp[np.newaxis, :]
                elif self._master_volume != 1.0:
                    output = output * self._master_volume
        # AM-09: 连续状态限幅
        output = self._master_limiter.process(output)
        return output
