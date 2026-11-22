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

from src.features.edit.core.audio_dsp import (
    MASTER_SINK_ID,
    EffectChain,
    StripEQFilter,
    apply_pdc_delay_line,
    plan_edge_pdc,
)
from src.features.edit.core.channel_layout import (
    ChannelLayout,
    downmix_matrix,
    routing_matrix,
)
from src.features.edit.core.audio_playback import topological_sort_buses, unpack_send

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


class _MasterFxShim:
    """R05：让 build_track_fx_sampler 复用于 Master 链的最小属性面。"""

    __slots__ = ("automation_mode", "effect_chain", "automation")

    def __init__(self, mode: str, chain: Any, automation: dict) -> None:
        self.automation_mode = mode
        self.effect_chain = chain
        self.automation = automation


def _build_master_fx_sampler(has_chain: bool, mode: str,
                             automation: dict, chain_params: Any) -> Any:
    """R05：从 master_automation 的 fx/* 键构建 Master 链采样器（可 None）。

    Master 与轨级共用 ``fx/{instance_id}/{param}`` 命名空间——同一实例
    id 在轨链与 Master 链中本就互斥（id 由创建处生成），不引入独立前缀。
    """
    if not has_chain:
        return None
    from src.features.edit.core.timeline_v2 import build_track_fx_sampler
    return build_track_fx_sampler(
        _MasterFxShim(mode, chain_params, automation or {}))


# ─────────────────────────────────────────────────────────────────────────────
# 节点配置：从 EditTrack 投影的不可变描述
# ─────────────────────────────────────────────────────────────────────────────

class _TrackNode:
    """单个 track/bus 的节点配置（从 EditTrack 投影）。

    持有该节点的静态参数（volume/pan/muted/solo/enabled/is_bus/output_bus_id/sends）
    与 DSP 处理器实例（EQ/FX，有状态 IIR）。automation 作为动态参数在 render 时求值。
    """

    __slots__ = (
        "track_id", "is_bus", "volume", "pan", "muted", "solo", "enabled", "solo_safe",
        "output_bus_id", "bus_layout", "sends", "automation", "automation_mode",
        "automation_trim", "eq_filter", "effect_chain", "panner", "fx_sampler",
    )

    def __init__(self, track: Any, sample_rate: float) -> None:
        self.track_id = getattr(track, "track_id", "")
        self.is_bus = bool(getattr(track, "is_bus", False))
        self.volume = float(getattr(track, "volume", 1.0))
        self.pan = float(getattr(track, "pan", 0.0))
        self.muted = bool(getattr(track, "is_muted", False))
        self.solo = bool(getattr(track, "is_solo", False))
        self.solo_safe = bool(getattr(track, "solo_safe", False))
        self.enabled = bool(getattr(track, "is_enabled", True))
        # AM-07: 自动化模式（off/read/write/touch/latch）
        self.automation_mode = str(getattr(track, "automation_mode", "off") or "off").lower()
        self.output_bus_id = getattr(track, "output_bus_id", None)
        # P2 环绕 Bus 路由核心：Bus 声道布局（轨→Bus 上混 / Bus→Master 降混
        # 走 channel_layout.routing_matrix；非 Bus 节点恒 stereo）。
        if getattr(track, "is_bus", False):
            layout_fn = getattr(track, "bus_channel_layout", None)
            self.bus_layout = (layout_fn() if callable(layout_fn)
                               else ChannelLayout.STEREO)
        else:
            self.bus_layout = ChannelLayout.STEREO
        # sends: [(target_bus_id, level, tap, muted), ...] 仅 enabled 的（MX-14 4 元组）
        raw_sends = getattr(track, "sends", ()) or ()
        self.sends = [
            (getattr(s, "target_bus_id", ""),
             float(getattr(s, "level", 0.0)),
             str(getattr(s, "tap", "post_fader") or "post_fader"),
             bool(getattr(s, "muted", False)))
            for s in raw_sends if getattr(s, "enabled", True)
        ]
        # AUD-15400: 深拷贝关键帧数据，切断节点与 UI 模型 KeyframeTrack 的活引用
        # （浅拷贝只隔离 dict 结构，对象仍共享，导出线程与 UI 编辑并发读写竞态）。
        self.automation = _snapshot_automation(getattr(track, "automation", {}))
        # B03：volume trim 线性倍率（模型 automation_trim 的离线消费）。
        # 仅 volume 参与 trim（pan/mute 无 trim 合同）；与实时 mix_samples
        # 同语义：乘在曲线值/静态值上各一次。非法值经模型 clamp 回 1.0。
        self.automation_trim = {
            "volume": float(getattr(track, "automation_trim_factor",
                                    lambda _p: 1.0)("volume")),
        }

        # DSP 处理器（有状态，按 track 独立）
        self.eq_filter = StripEQFilter(sample_rate)
        strip_eq = getattr(track, "strip_eq", None)
        if strip_eq is not None:
            self.eq_filter.set_params(strip_eq, sample_rate=sample_rate)

        self.effect_chain = EffectChain(sample_rate)
        chain = getattr(track, "effect_chain", None)
        if chain is not None and hasattr(chain, "to_dsp_entries"):
            self.effect_chain.set_params(chain.to_dsp_entries(), sample_rate=sample_rate)
        # MX-10: fx 参数自动化采样器（frame -> entries；mode=off 或无 fx 键 → None）
        from src.features.edit.core.timeline_v2 import build_track_fx_sampler
        self.fx_sampler = build_track_fx_sampler(track)

        # CW-A1: 空间声像器（立体声域矩阵；lr 真源是上面的 track.pan）
        from src.features.edit.core.audio_dsp import SpatialPanner
        self.panner = SpatialPanner(sample_rate)
        panner_params = getattr(track, "panner", None)
        if panner_params:
            self.panner.set_params(panner_params, sample_rate=sample_rate)


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
        # MX-03: Master 条带（EQ/FX；音量后、限幅器前，与 mix_samples 同构）
        self._master_eq: Optional[StripEQFilter] = None
        self._master_chain: Optional[EffectChain] = None
        # P0/B02 · PDC 状态（configure_from_sequence 重算；render 前为空恒等）
        self._pdc_track_max = 0
        self._pdc_bus_max = 0
        self._pdc_master_lat = 0
        self._pdc_total = 0
        self._pdc_arrival: dict[str, int] = {}
        self._pdc_delay: dict[str, int] = {}
        self._pdc_send_edges: dict[str, int] = {}
        self._pdc_lines: dict[str, np.ndarray] = {}
        # P0 · Clip FX PDC：每轨 clip 链最大算法延迟（导出循环经
        # note_clip_chain_latency 申报；configure 时未知 → 空 = 恒等）。
        # 轨节点延迟 = 轨链延迟 + clip 最大延迟；同轨 clip 间由
        # _clip_pdc_lines（键=clip_id，经 render_track_block 推入）对齐。
        self._track_clip_lat: dict[str, int] = {}
        self._clip_pdc_lines: dict[str, np.ndarray] = {}
        # P1 · VCA 倍率缓存（未 configure 的渲染器恒等）。
        self._vca_factors: dict = {}
        # R05: Master fx 参数自动化采样器（configure_from_sequence 构建）
        self._master_fx_sampler = None
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
        # MX-03: Master 条带（EQ/FX）。旧序列无这两个字段 → None（直通）。
        master_eq_params = getattr(sequence, "master_strip_eq", None)
        if master_eq_params is not None:
            self._master_eq = StripEQFilter(self._sample_rate)
            self._master_eq.set_params(master_eq_params,
                                       sample_rate=self._sample_rate)
        else:
            self._master_eq = None
        master_chain_params = getattr(sequence, "master_effect_chain", None)
        if master_chain_params is not None:
            self._master_chain = EffectChain(self._sample_rate)
            self._master_chain.set_params(
                master_chain_params.to_dsp_entries(),
                sample_rate=self._sample_rate)
        else:
            self._master_chain = None
        # R05: Master fx 参数自动化——与轨级 fx/{instance_id}/{param} 同一
        # 命名空间（master_automation dict），apply_master 块边界消费。
        self._master_fx_sampler = _build_master_fx_sampler(
            self._master_chain is not None,
            str(getattr(sequence, "master_automation_mode", "off") or "off"),
            self._master_automation,
            master_chain_params,
        )
        # P0 · PDC（插件延迟补偿）：两级"最迟路径对齐"——轨链一级、Bus 链
        # 一级（Master 链作用于汇合后的共同信号，不产生相对错位）。每个
        # 节点输出延迟 max(同级链延迟) − 本链延迟，声学事件在 Master 汇合
        # 处落在同一绝对样本；延迟量计入尾音预算（延迟线需 flush）。
        self._recompute_pdc()
        # P1 · VCA：成员轨推子级增益/静音（路由与 Send tap 不变）。
        self._vca_factors = {
            track.track_id: float(getattr(sequence, "vca_factor_for",
                                           lambda _tid: 1.0)(track.track_id))
            for track in audio_tracks
            if not getattr(track, "is_bus", False)
        }

    def vca_factor_for(self, track_id: str) -> float:
        """成员轨的 VCA 倍率（configure 后缓存；非成员=1）。"""
        return self._vca_factors.get(track_id, 1.0)

    # ── P0/B02 · PDC：DAG 边级延迟规划与输出端延迟线 ─────────────────

    def _node_latency_samples(self, node_id: str) -> tuple[int, int]:
        """节点固有延迟 (chain_lat, clip_lat)（规划与 tap 语义共用）。"""
        node = self._nodes.get(node_id)
        chain = getattr(node, "effect_chain", None) if node else None
        chain_lat = 0
        if chain is not None:
            try:
                chain_lat = int(chain.latency_samples())
            except Exception:  # noqa: BLE001 — 声明异常按 0
                chain_lat = 0
        clip_lat = int(self._track_clip_lat.get(node_id, 0) or 0)
        return max(0, chain_lat), max(0, clip_lat)

    def _recompute_pdc(self) -> None:
        """B02：DAG 边级 PDC 规划（取代旧"轨级 max + Bus 级 max"两级近似）。

        每个节点声明固有延迟（链 + clip 最大）；图展开为边（主路输出边
        + 每条 send 独立边，pre/post tap 按取样内容携带的延迟计），
        `plan_edge_pdc` 计算每条边的补偿量；同一源的各路径在共同下游
        汇合点对齐。Bus 环（含环下游，topological_sort_buses 剔除的集合）
        不参与渲染，其边不参与规划。

        延迟线键：主路边 = node_id（兼容既有 _apply_pdc/_pdc_lines 语义），
        send 边 = "node|s{i}|target"（同 (u, v) 的多条 send 各自独立线）。
        """
        # 1) 节点固有延迟
        lat: dict[str, int] = {}
        chain_lat: dict[str, int] = {}
        clip_lat: dict[str, int] = {}
        for nid in self._nodes:
            c_lat, k_lat = self._node_latency_samples(nid)
            chain_lat[nid] = c_lat
            clip_lat[nid] = k_lat
            lat[nid] = c_lat + k_lat
        master_lat = 0
        if self._master_chain is not None:
            try:
                master_lat = int(self._master_chain.latency_samples())
            except Exception:  # noqa: BLE001
                master_lat = 0
        master_lat = max(0, master_lat)
        lat[MASTER_SINK_ID] = master_lat

        # 2) Bus 环检测（与 render_bus_stage 同一拓扑排序 → 同一剔除集合）
        bus_ids = [nid for nid, n in self._nodes.items() if n.is_bus]
        _sorted_buses, cycle_buses = topological_sort_buses(
            bus_ids, self._build_routing_dict())
        cycle = set(cycle_buses)

        # 3) 边展开（主路 + sends；环内节点与其入边不参与）
        edges: list[tuple[str, str, str, int]] = []
        send_keys: set[str] = set()
        for nid, node in self._nodes.items():
            if nid in cycle:
                continue
            target = node.output_bus_id
            if target is not None and (target not in self._nodes
                                       or target in cycle):
                # AUD-15402 兜底语义：未知/环目标 → master（渲染路由一致）
                target = MASTER_SINK_ID
            elif target is None:
                target = MASTER_SINK_ID
            # 主路边：tap = 节点链输出（tap_latency = 全节点延迟）
            edges.append((nid, target, nid, lat[nid]))
            # send 边：pre_fader tap 取 clip 求和块（携带 clip 延迟），
            # post_fader tap 取链输出（携带全节点延迟）。
            for idx, send_entry in enumerate(node.sends):
                send_target, send_level, send_tap, send_muted = (
                    unpack_send(send_entry))
                if send_level <= 0.0 or send_muted:
                    continue
                if (send_target not in self._nodes or send_target in cycle
                        or not self._nodes[send_target].is_bus):
                    continue  # 渲染路由会跳过：不参与规划
                key = f"{nid}|s{idx}|{send_target}"
                tap_latency = clip_lat[nid] if send_tap == "pre_fader" else lat[nid]
                edges.append((nid, send_target, key, tap_latency))
                send_keys.add(key)

        # 4) 规划（单边上限 60s @ 当前采样率；超限截断防无界内存）
        max_delay = int(self._sample_rate * 60)
        edge_delay, arrival, total = plan_edge_pdc(
            lat, edges, MASTER_SINK_ID, max_edge_delay=max_delay)
        for key, delay in edge_delay.items():
            if delay >= max_delay:
                logger.error(
                    "AudioGraphRenderer: PDC edge %r delay %d samples hit the "
                    "%ds cap; output alignment for this edge is clamped",
                    key, delay, int(max_delay / max(1, self._sample_rate)))
        self._pdc_track_max = 0  # 旧字段保留为 0（诊断接口已改用 arrival）
        self._pdc_bus_max = 0
        self._pdc_master_lat = master_lat
        self._pdc_total = int(total)
        self._pdc_arrival = dict(arrival)
        # 主路边延迟（键=node_id；仅记录 >0，消费方 get(node, 0) 语义不变）
        self._pdc_delay = {}
        for nid in self._nodes:
            delay = int(edge_delay.get(nid, 0))
            if delay > 0:
                self._pdc_delay[nid] = delay
        # send 边延迟（键=边 key "node|s{i}|target"；路由点按 enumerate
        # 索引定位，与规划同一枚举口径）
        self._pdc_send_edges = {
            key: int(edge_delay.get(key, 0)) for key in send_keys
        }
        # 延迟线缓冲（feed 后保留尾部，输出为"缓冲 + 新块"的前段）。
        self._pdc_lines = {}
        # P0 · Clip FX PDC：轨级 clip 延迟申报变化 → 相对延迟线一并清空
        # （调用时序：导出循环在块循环前申报；渲染中途不重入）。
        self._clip_pdc_lines = {}

    def pdc_out_edges_max_delay(self, node_id: str) -> int:
        """该节点出边（主路 + 全部 send 边）的最大 PDC 延迟（样本）。

        导出循环的间隙零喂预算用：FX 尾音预算之外，边延迟线里的持留
        内容也必须按绝对时间继续流出（预算 = 链尾音 + 本值）。
        """
        delay = int(self._pdc_delay.get(node_id, 0))
        prefix = f"{node_id}|s"
        for key, edge_delay in self._pdc_send_edges.items():
            if key.startswith(prefix):
                delay = max(delay, int(edge_delay))
        return delay

    def is_known_bus(self, bus_id: Optional[str]) -> bool:
        """bus_id 是否为节点图中已注册的 Bus（AUD-15402 判定的公开接口）。"""
        return self._is_known_bus(bus_id)

    def note_clip_chain_latency(self, track_id: str, latency_samples: int) -> None:
        """申报某轨 clip FX 链的最大算法延迟（P0 · Clip FX PDC）。

        导出循环在块循环前调用（每轨一次，传该轨全部 clip 链延迟的
        最大值；无延迟声明传 0）。轨节点 PDC 随之重算：轨节点延迟 =
        轨链延迟 + clip 最大延迟——同轨 clip 间由 render_track_block 的
        相对延迟线对齐，轨级与其他轨在 Master 汇合处同样本落点。
        """
        lat = max(0, int(latency_samples or 0))
        if lat > 0:
            self._track_clip_lat[str(track_id)] = lat
        else:
            self._track_clip_lat.pop(str(track_id), None)
        self._recompute_pdc()

    def pdc_total_samples(self) -> int:
        """主 sink 的总算法延迟（arrival[Master] + Master 链声明）。

        尾音/输出长度预算必须计入本值——延迟线把内容推迟这么多样本，
        drain 不延长会截掉尾部内容。B02：取代旧"轨级 max + Bus 级 max"
        两级近似，等于最迟路径的真实补偿总量。
        """
        return int(getattr(self, "_pdc_total", 0))

    def reset_pdc_state(self) -> None:
        """seek/重开：清空延迟线（避免残留内容错位）。"""
        self._pdc_lines = {}

    def _apply_pdc(self, node_id: str, samples: np.ndarray) -> np.ndarray:
        """主路边延迟线（键=node_id；render_track_block 输出对齐到该节点
        主路目标的到达时间）。"""
        return apply_pdc_delay_line(
            self._pdc_lines, node_id, self._pdc_delay.get(node_id, 0), samples)

    def _send_edge_key(self, node_id: str, send_index: int,
                       send_target: str) -> str:
        return f"{node_id}|s{int(send_index)}|{send_target}"

    def apply_send_edge_delay(self, node_id: str, send_index: int,
                              send_target: str, samples: np.ndarray) -> np.ndarray:
        """send 边延迟线（公共接口；导出循环/实时混音在 send 路由点调用）。

        send 内容与主路在同一节点输出取样（pre/post tap 语义由规划期的
        tap_latency 区分），经本边线推迟到该 send 目标的到达时间。
        """
        key = self._send_edge_key(node_id, send_index, send_target)
        return apply_pdc_delay_line(
            self._pdc_lines, key,
            int(self._pdc_send_edges.get(key, 0)), samples)

    def advance_node_pdc_edges(self, node_id: str, num_samples: int) -> None:
        """节点整块无声（mute/被跳过）时推进其全部出边延迟线（喂零）。

        被推迟的内容必须按绝对时间继续流出；线宽取既有缓冲的行数
        （空线无内容可推进，等实际内容流动时按其宽度建立）。
        """
        delay = self._pdc_delay.get(node_id, 0)
        if delay > 0:
            buffered = self._pdc_lines.get(node_id)
            if buffered is not None and buffered.shape[-1] > 0:
                apply_pdc_delay_line(
                    self._pdc_lines, node_id, delay,
                    np.zeros((buffered.shape[0], num_samples), dtype=np.float32))
        for key, edge_delay in self._pdc_send_edges.items():
            if not key.startswith(f"{node_id}|s"):
                continue
            if edge_delay <= 0:
                continue
            buffered = self._pdc_lines.get(key)
            if buffered is None or buffered.shape[-1] == 0:
                continue
            apply_pdc_delay_line(
                self._pdc_lines, key, edge_delay,
                np.zeros((buffered.shape[0], num_samples), dtype=np.float32))

    def master_sidechain_sources(self) -> set:
        """MX-03: Master 链引用的外部侧链检源 track_id 集合（导出拼 tap 用）。"""
        if self._master_chain is not None:
            try:
                return set(self._master_chain.sidechain_sources())
            except Exception:  # noqa: BLE001 — 诊断性查询失败按空集处理
                return set()
        return set()

    def master_tail_estimate_samples(self) -> int:
        """R04: Master FX 链尾音预算（样本；无链/旁路 = 0）。

        含尾导出模式据此延长输出缓冲；严格选区模式不消费本值。
        """
        if self._master_chain is not None:
            try:
                return int(self._master_chain.tail_estimate_samples())
            except Exception:  # noqa: BLE001 — 诊断性查询失败按 0 处理
                return 0
        return 0

    def bus_tail_estimate_samples(self) -> int:
        """R04: 全部 Bus 节点链尾音预算的最大值（样本；并联汇合取 max）。"""
        best = 0
        for node in self._nodes.values():
            if node.is_bus and node.effect_chain is not None:
                try:
                    best = max(best, int(node.effect_chain.tail_estimate_samples()))
                except Exception:  # noqa: BLE001
                    continue
        return best

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
            [nid for nid, node in self._nodes.items() if node.solo_safe],
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
        sidechain_taps: Optional[dict] = None,
        clip_pdc_delay: int = 0,
        clip_pdc_key: str = "",
    ) -> Optional[np.ndarray]:
        """处理单个普通轨的一个样本块。

        节点顺序（与 AudioMixer.mix_samples 阶段 1 一致）：
        clip fade → automation(mute gate) → clip FX → automation(volume/pan)
        → volume → pan → spatial panner → strip EQ → track FX

        Args:
            track_id: 轨道 ID
            samples: 解码后的样本 (channels, n)
            current_frame: 当前时间线帧（automation 求值用）
            clip_fx_chain: 该 clip 的 FX 链（可选，pre-fader）
            clip_pdc_delay: P0 · Clip FX PDC——本 clip 相对同轨最大 clip 链
                延迟的差值（track_clip_max − 本链 latency；>0 时经
                clip_pdc_key 定位的持久延迟线推迟，与轨节点 PDC 同语义）。
            clip_pdc_key: 相对延迟线的稳定键（clip_id；空 = 不建线）。
            clip_fade: clip 淡化参数 dict，含 keys:
                timeline_start, timeline_duration, fade_in_frames, fade_out_frames, fps。
                提供 AM-06 clip 淡入淡出（在所有其他节点之前）。
            source_channels: RMA-08 源素材原始声道数（1=mono→equal-power pan，
                2=stereo→balance unity-center，>2=surround→trim）。默认 2。
            fps: 时间线帧率（P1-20：automation ramp 的帧→样本换算基准）。
                显式传入优先；未传时回退 clip_fade["fps"]，再退 24.0。
            sidechain_taps: CW-A1 外部侧链检源缓冲 {source_track_id: (ch, n)}，
                None=内部检源。当前导出循环按"轨-外层"迭代，跨轨 tap 不可
                用 → 默认内部检源（压缩器仍正常工作）。

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
                overlap_regions=clip_fade.get("overlap_regions", ()),
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
                    adjustments=xf.get("adjustments", (0.0, 0.0)),
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
                    # P0/B02 · PDC：整块静音也要喂零推进该节点全部出边
                    # 延迟线（被推迟内容按绝对时间继续流出）。
                    self.advance_node_pdc_edges(track_id, samples.shape[-1])
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
        # P0 · Clip FX PDC：同轨 clip 间相对对齐（与实时 mix_samples 同
        # 语义；轨节点整体推迟已计入 _track_clip_lat 重算）。对全部 clip
        # 生效：无链/旁路/失活链无算法延迟按 0 计（内容本无位移，须补满
        # clip_max 才能与其余轨同样本汇合）。
        if clip_pdc_delay > 0 and clip_pdc_key:
            samples = apply_pdc_delay_line(
                self._clip_pdc_lines, str(clip_pdc_key),
                int(clip_pdc_delay), samples)

        # volume（§3.2: ramp 时逐样本）；B03：trim 倍率与 P1 · VCA 推子级
        # 倍率一并应用（trim×VCA 各乘一次，pre-fader send 的 tap 在轨链
        # 之前，不受二者影响）。与模型 get_automation_value 同语义。
        vca_factor = self.vca_factor_for(track_id)
        trim_factor = node.automation_trim.get("volume", 1.0)
        if volume_ramp is not None:
            samples = samples * ((volume_ramp * trim_factor)
                                 * vca_factor)[np.newaxis, :]
        else:
            samples = samples * (volume * trim_factor * vca_factor)

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

        # CW-A1: 空间声像器（spread/rotate/divergence/boom 立体声域矩阵，
        # 与 mix_samples 同级：pan/balance 增益之后、strip EQ 之前）
        if node.panner is not None and node.panner.is_active:
            if pan_ramp is not None and len(pan_ramp) > 0:
                mid = min(len(pan_ramp) - 1, samples.shape[-1] // 2)
                pan_eff = float(np.clip(pan_ramp[mid], -1.0, 1.0))
            else:
                pan_eff = pan
            samples = node.panner.process(samples, pan_eff)

        # strip EQ（post-fader）
        if node.eq_filter is not None and node.eq_filter.is_active:
            samples = node.eq_filter.process(samples)

        # track FX（post-EQ；CW-A1: 外部侧链检源透传）
        # A neutral chain may become active at this block's keyframe. Sample
        # first; process() itself skips inactive nodes and clears stale taps.
        if node.effect_chain is not None:
            # MX-10: fx 参数自动化——块边界按当前帧重设链参数（与实时
            # mix_samples / clip FX 的 animated_sampler 同型）
            if node.fx_sampler is not None:
                try:
                    node.effect_chain.set_params(
                        node.fx_sampler(int(current_frame)),
                        sample_rate=self._sample_rate)
                except Exception:  # noqa: BLE001 — 曲线求值失败回退静态
                    logger.warning(
                        "AudioGraphRenderer: fx automation sampler failed "
                        "for track %s", track_id, exc_info=True)
                    node.fx_sampler = None
            sc_map = None
            if sidechain_taps:
                raw_map = node.effect_chain.sidechain_map(sidechain_taps)
                if raw_map:
                    cur_n = samples.shape[-1]
                    sc_map = {iid: buf[:, :cur_n] for iid, buf in raw_map.items()}
            samples = node.effect_chain.process(samples, sidechain_by_id=sc_map)

        # 与实时路径保持一致：mute gate 位于 strip 输出端，静音时不泄漏 FX 尾音。
        if mute_gate is not None:
            samples = samples * mute_gate[np.newaxis, :samples.shape[-1]]

        # B02 · PDC：节点处理到此为止（未延迟）。主路/send 各边在路由点
        # 分别经独立延迟线对齐到各自目标的到达时间（route_track_output /
        # 导出循环的 send 路由）。
        return samples

    # ── 路由：把处理后样本累积到 bus_inputs 或 master ──

    def _is_known_bus(self, bus_id: Optional[str]) -> bool:
        """AUD-15402: bus_id 是否为节点图中已注册的 Bus 节点。"""
        if not bus_id:
            return False
        node = self._nodes.get(bus_id)
        return node is not None and node.is_bus

    def _bus_layout_of(self, bus_id: str) -> ChannelLayout:
        """Bus 节点的声道布局（未知/非 Bus 回退 stereo）。"""
        node = self._nodes.get(bus_id)
        return (node.bus_layout if node is not None else ChannelLayout.STEREO)

    def _new_bus_buffer(self, bus_id: str, reference: np.ndarray) -> np.ndarray:
        """按 Bus 布局分配空缓冲（宽度/ dtype 对齐参考 master 缓冲）。"""
        layout = self._bus_layout_of(bus_id)
        return np.zeros((layout.channel_count, reference.shape[-1]),
                        dtype=reference.dtype)

    def _downmix_to_master(self, samples: np.ndarray,
                           src_layout: ChannelLayout,
                           dst_channels: int = 2) -> np.ndarray:
        """Bus → Master 降混：BS.775，LFE 默认排除。

        ``dst_channels`` 来自实际 master 缓冲宽度（单声道 stem=1、
        立体声=2）；不可推断的宽度原样返回（不虚构映射）。
        """
        if src_layout.channel_count == dst_channels:
            return samples
        try:
            dst = ChannelLayout.from_channel_count(dst_channels)
        except ValueError:
            return samples
        matrix = downmix_matrix(src_layout, dst, include_lfe=False)
        return (matrix @ samples.astype(np.float64)).astype(samples.dtype)

    def _upmix_to_bus(self, samples: np.ndarray, src_layout: ChannelLayout,
                      bus_id: str) -> np.ndarray:
        """轨/上游 Bus 立体声样本 → 目标 Bus 布局（routing_matrix 上混）。

        同布局直通返回原数组（零拷贝）；上混为静态矩阵乘。
        """
        dst = self._bus_layout_of(bus_id)
        if dst.channel_count == samples.shape[0]:
            return samples
        matrix = routing_matrix(src_layout, dst)
        return (matrix @ samples.astype(np.float64)).astype(samples.dtype)

    def route_track_output(
        self,
        track_id: str,
        samples: np.ndarray,
        output: np.ndarray,
        bus_inputs: dict,
        pre_fader_samples: Optional[np.ndarray] = None,
    ) -> None:
        """把普通轨处理后的样本路由到 bus_inputs 或 master（与 mix_samples 一致）。

        B02 · PDC：``samples`` 为未延迟的节点输出；主路边与每条 send 边
        在此分别经独立延迟线对齐到各自目标的到达时间。

        output_bus_id 非 None → 累积到 bus_inputs[output_bus_id]
        并行处理 sends → bus_inputs[send_bus_id] *= level
        MX-14: pre_fader send 消费 pre_fader_samples（轨链前输入）；未提供时
        降级用处理后样本并告警（不静默给错 tap 位置）。
        """
        node = self._nodes.get(track_id)
        target = node.output_bus_id if node else None
        n = samples.shape[-1]
        # B02: 主路边延迟（键=node_id；未知目标兜底 master 与规划一致）
        routed = self._apply_pdc(track_id, samples)
        if target is not None:
            if self._is_known_bus(target):
                if target not in bus_inputs:
                    bus_inputs[target] = self._new_bus_buffer(target, output)
                bus_inputs[target][:, :n] += self._upmix_to_bus(
                    routed, node.bus_layout if node else ChannelLayout.STEREO,
                    target)
            else:
                # AUD-15402: 未知 bus 目标（如 bus 已删除但轨的路由未清理）。
                # 旧代码为未知目标也创建缓冲——该轨样本累积进永不被消费的
                # 孤儿缓冲，整轨音频在导出中静默丢失。与 Bus 阶段（render_bus_stage
                # 的路由兜底分支）语义统一：兜底进 master 并告警。
                logger.warning(
                    "AudioGraphRenderer: track %r routes to unknown bus %r; "
                    "falling back to master", track_id, target,
                )
                output[:, :n] += routed
        else:
            output[:, :n] += routed
        # sends（并行；MX-14 4 元组 + 独立 mute + pre/post tap）
        if node is not None:
            for send_index, send_entry in enumerate(node.sends):
                send_bus_id, send_level, send_tap, send_muted = (
                    unpack_send(send_entry))
                if send_level <= 0.0 or send_muted:
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
                    bus_inputs[send_bus_id] = self._new_bus_buffer(
                        send_bus_id, output)
                src_layout = node.bus_layout if node else ChannelLayout.STEREO
                if send_tap == "pre_fader":
                    if pre_fader_samples is None:
                        logger.warning(
                            "AudioGraphRenderer: track %r has pre_fader send to "
                            "%r but no pre-fader source provided; falling back "
                            "to post-fader tap", track_id, send_bus_id,
                        )
                        src = samples
                    else:
                        src = pre_fader_samples
                    sn = min(src.shape[-1], n)
                    src_delayed = self.apply_send_edge_delay(
                        track_id, send_index, send_bus_id, src)
                    bus_inputs[send_bus_id][:, :sn] += (
                        self._upmix_to_bus(src_delayed, src_layout, send_bus_id)
                        [:, :sn] * send_level)
                else:
                    send_delayed = self.apply_send_edge_delay(
                        track_id, send_index, send_bus_id, samples)
                    bus_inputs[send_bus_id][:, :n] += (
                        self._upmix_to_bus(send_delayed, src_layout, send_bus_id)
                        * send_level)

    # ── Bus 阶段（拓扑序，与 mix_samples 阶段 2 一致）──

    def render_bus_stage(
        self,
        bus_inputs: dict,
        output: np.ndarray,
        has_solo: bool,
        audible_solo: set = None,
        start_frame: int = 0,
        fps: float = 24.0,
        sidechain_taps: Optional[dict] = None,
    ) -> None:
        """按拓扑序处理 Bus，累积到 output（与 mix_samples 阶段 2 一致）。

        修改 output in-place（Bus 处理后路由到下游 bus 或 master）。

        Args:
            audible_solo: RMA-04 预计算的 solo 可听闭包（None 时内部计算）。
                Bus 必须在闭包内才保留，替代旧的 `not node.solo` 单点判断。
            start_frame/fps: RMA-06 Bus automation（volume/pan/mute）逐样本求值用。
                Bus automation 此前完全被忽略（UI/模型可保存却无声跳过）。
            sidechain_taps: MX-08 Bus 压缩器外部检源缓冲 {source_id: (ch, n)}
                （与普通轨 render_track_block 同构）。
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

        # 确保每个 Bus 都有 buffer（按 Bus 布局分配；P2 环绕 Bus 路由）
        for bid in bus_ids:
            if bid not in bus_inputs:
                bus_inputs[bid] = self._new_bus_buffer(bid, output)


        # H02：Bus 阶段逐帧块内处理——volume/pan/mute ramp 与 fx 参数自动化
        # 按每帧块的起始帧求值（与实时 mix_samples 的帧粒度一致）。旧实现
        # 对整段 buffer 只按 start_frame 采一次：f0=0、f10=−12dB 的电平曲线
        # 在 Bus/Master 成片 f20 仍输出 ~0.2（复审 §5 H02 已复现）。
        n_total = output.shape[-1]
        chunk = max(1, int(round(self._sample_rate / max(1e-6, float(fps)))))
        from src.features.edit.core.audio_dsp import balance_gains

        for bus_id in sorted_buses:
            bus_input_full = bus_inputs.get(bus_id)
            if bus_input_full is None:
                continue
            node = self._nodes.get(bus_id)
            if node is None or not node.enabled or node.muted:
                continue
            # RMA-04: 用闭包判断，替代 `not node.solo`
            if has_solo and bus_id not in audible_solo:
                continue

            auto = node.automation if node.automation_mode != "off" else None
            vol_track = (auto or {}).get("volume")
            pan_track = (auto or {}).get("pan")
            mute_track = (auto or {}).get("mute")
            has_vol = vol_track is not None and getattr(vol_track, "keyframes", None)
            has_pan = pan_track is not None and getattr(pan_track, "keyframes", None)
            has_mute = mute_track is not None and getattr(mute_track, "keyframes", None)

            for c0 in range(0, n_total, chunk):
                cN = min(n_total, c0 + chunk)
                n = cN - c0
                frame_c = float(start_frame) + c0 / max(
                    1.0, self._sample_rate / max(1e-6, float(fps)))
                bus_input = bus_input_full[:, c0:cN]

                # RMA-06/RMA-07：逐帧块 automation（mute 门控 + de-click）
                if has_mute:
                    mute_ramp = np.array(
                        mute_track.get_value_ramp(
                            frame_c, fps, n, self._sample_rate, 0.0),
                        dtype=output.dtype)
                    gate = (mute_ramp <= 0.5).astype(output.dtype)
                    gate = self._apply_declick_edges(gate, self._sample_rate)
                    bus_input = bus_input * gate[np.newaxis, :]

                # volume（automation ramp 或静态）
                if has_vol:
                    vol_ramp = np.array(
                        vol_track.get_value_ramp(
                            frame_c, fps, n, self._sample_rate, node.volume),
                        dtype=output.dtype)
                    bus_samples = bus_input * vol_ramp[np.newaxis, :]
                else:
                    bus_samples = bus_input * node.volume
                # pan（RMA-08: balance unity-center；逐样本 ramp 或块起点标量）
                if has_pan and bus_samples.shape[0] >= 2:
                    pan_ramp = np.array(
                        pan_track.get_value_ramp(
                            frame_c, fps, n, self._sample_rate, node.pan),
                        dtype=output.dtype)
                    bus_samples = bus_samples.copy()
                    for i in range(min(len(pan_ramp), bus_samples.shape[-1])):
                        lg, rg = balance_gains(float(pan_ramp[i]))
                        bus_samples[0, i] *= lg
                        bus_samples[1, i] *= rg
                    pan_scalar = float(pan_ramp[0]) if len(pan_ramp) else node.pan
                else:
                    lg, rg = balance_gains(node.pan)
                    bus_samples = bus_samples.copy()
                    bus_samples[0] *= lg
                    if bus_samples.shape[0] > 1:
                        bus_samples[1] *= rg
                    pan_scalar = node.pan
                # CW-A1: Bus 空间声像器（与 mix_samples Bus 阶段同级）
                if node.panner is not None and node.panner.is_active:
                    bus_samples = node.panner.process(bus_samples, pan_scalar)
                # EQ
                if node.eq_filter is not None and node.eq_filter.is_active:
                    bus_samples = node.eq_filter.process(bus_samples)
                # FX（MX-08: 外部侧链检源透传；R05/H02：fx 自动化按帧块采样）
                if node.effect_chain is not None:
                    if node.fx_sampler is not None:
                        try:
                            node.effect_chain.set_params(
                                node.fx_sampler(int(round(frame_c))),
                                sample_rate=self._sample_rate)
                        except Exception:  # noqa: BLE001 — 曲线求值失败回退静态
                            logger.warning(
                                "AudioGraphRenderer: fx automation sampler "
                                "failed for bus %s", bus_id, exc_info=True)
                            node.fx_sampler = None
                    sc_map = None
                    if sidechain_taps:
                        raw_map = node.effect_chain.sidechain_map(sidechain_taps)
                        if raw_map:
                            sc_map = {iid: buf[:, c0:cN]
                                      for iid, buf in raw_map.items()}
                    bus_samples = node.effect_chain.process(
                        bus_samples, sidechain_by_id=sc_map)
                # B02 · PDC：bus_samples 保持未延迟；主路边/send 边在路由点
                # 分别经独立延迟线对齐（跨块状态保存在延迟线里）。
                # 路由：Bus→Bus 或 →Master（output_bus_id）。P2 环绕
                # Bus 路由：跨布局走 routing_matrix（上混/降混；Bus→Master
                # 为 BS.775 降混，LFE 默认排除）。
                target = node.output_bus_id
                if target is not None and target in bus_inputs:
                    bus_delayed = self._apply_pdc(bus_id, bus_samples)
                    bus_inputs[target][:, c0:cN] += self._upmix_to_bus(
                        bus_delayed, node.bus_layout, target)
                else:
                    if target is not None and not self._is_known_bus(target):
                        # AUD-15402: Bus 输出指向未注册 bus——兜底进 master 并告警
                        logger.warning(
                            "AudioGraphRenderer: bus %r routes to unknown bus %r; "
                            "falling back to master", bus_id, target,
                        )
                    bus_delayed = self._apply_pdc(bus_id, bus_samples)
                    output[:, c0:cN] += self._downmix_to_master(
                        bus_delayed, node.bus_layout, output.shape[0])
                # RMA-06: Bus sends（并行 tap；MX-14: pre_fader 取 Bus 输入）。
                for send_index, send_entry in enumerate(node.sends):
                    send_bus_id, send_level, send_tap, send_muted = (
                        unpack_send(send_entry))
                    if send_level <= 0.0 or send_muted:
                        continue
                    if send_bus_id in bus_inputs:
                        if send_tap == "pre_fader":
                            # B02: pre tap = Bus 输入（未过链），经该 send
                            # 边的 pre 延迟线（规划期 tap_latency=0）。
                            send_src = self.apply_send_edge_delay(
                                bus_id, send_index, send_bus_id, bus_input)
                        else:
                            send_src = self.apply_send_edge_delay(
                                bus_id, send_index, send_bus_id, bus_samples)
                        bus_inputs[send_bus_id][:, c0:cN] += \
                            self._upmix_to_bus(send_src, node.bus_layout,
                                               send_bus_id) * send_level
                    elif not self._is_known_bus(send_bus_id):
                        # AUD-15402: Bus send 指向未注册 bus——并行 tap 落空，告警
                        logger.warning(
                            "AudioGraphRenderer: bus %r send targets unknown bus %r; "
                            "send skipped", bus_id, send_bus_id,
                        )

    # ── Master 阶段（与 mix_samples 阶段 3 一致）──

    def apply_master(self, output: np.ndarray, skip_volume: bool = False,
                     start_frame: int = 0, fps: float = 24.0,
                     sidechain_taps: Optional[dict] = None) -> np.ndarray:
        """应用 master volume/mute + Master 条带（EQ/FX）+ 连续状态限幅。

        AM-09: 用连续状态 MasterLimiter 替代无状态峰值归一化，
        跨 buffer 平滑增益包络，避免 block-boundary gain jump（pumping）。

        §3.1: master automation（volume ramp + mute 判定），需 start_frame/fps。
        MX-03: master strip_eq → master FX（音量后、限幅器前，与 mix_samples
        阶段 3 同构）；sidechain_taps 为 {source_track_id: ndarray} 检源池。

        Args:
            skip_volume: 跳过 master volume/mute/automation，仍应用条带和限幅。
                已处理母带的归一化保护应调用 limit_master_output，避免重复运行效果链。
            start_frame: output 起始帧（master automation ramp 用）
            fps: 帧率（master automation ramp 用）
            sidechain_taps: Master FX 链的外部侧链检源输出（导出方拼接）
        """
        master_gate: Optional[np.ndarray] = None
        static_muted = False
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
                        master_gate = (mute_ramp <= 0.5).astype(output.dtype)
                        master_gate = self._apply_declick_edges(master_gate, self._sample_rate)
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
        # MX-03: Master 条带——EQ → FX（音量后、限幅器前）。H02：逐帧块内
        # 处理，fx 参数自动化按每帧块起始帧采样（旧实现整段只采 start_frame
        # 一次——变化曲线在成片/Stem 里冻结在起点值，复审 §5 H02 已复现）。
        n_total = output.shape[-1]
        _chunk = max(1, int(round(self._sample_rate / max(1e-6, float(fps)))))
        if self._master_eq is not None and self._master_eq.is_active:
            output = self._master_eq.process(output)
        if self._master_chain is not None:
            for c0 in range(0, n_total, _chunk):
                cN = min(n_total, c0 + _chunk)
                frame_c = float(start_frame) + c0 / max(
                    1.0, self._sample_rate / max(1e-6, float(fps)))
                if self._master_fx_sampler is not None:
                    try:
                        self._master_chain.set_params(
                            self._master_fx_sampler(int(round(frame_c))),
                            sample_rate=self._sample_rate)
                    except Exception:  # noqa: BLE001 — 曲线求值失败回退静态
                        logger.warning(
                            "AudioGraphRenderer: master fx automation sampler "
                            "failed", exc_info=True)
                        self._master_fx_sampler = None
                sc_map = None
                if sidechain_taps:
                    raw_map = self._master_chain.sidechain_map(sidechain_taps)
                    if raw_map:
                        sc_map = {iid: buf[:, c0:cN]
                                  for iid, buf in raw_map.items()}
                output[:, c0:cN] = self._master_chain.process(
                    output[:, c0:cN], sidechain_by_id=sc_map)
        # Stateful effects can emit past audio even when their input is zero.
        # The final Master mute must gate those tails as well as direct audio.
        if static_muted:
            output = output * 0.0
        elif master_gate is not None:
            output = output * master_gate[np.newaxis, :]
        # AM-09: 连续状态限幅
        return self.limit_master_output(output)

    def limit_master_output(self, output: np.ndarray, *,
                            reset_state: bool = False) -> np.ndarray:
        """Protect rendered PCM without reapplying Master gain, EQ, or effects.

        A separate offline pass (for example after loudness normalization)
        starts at sample zero and must not inherit the previous pass's final
        gain envelope. Streaming callers keep the continuous state by default.
        """
        if reset_state:
            self._master_limiter.reset()
        return self._master_limiter.process(output)
