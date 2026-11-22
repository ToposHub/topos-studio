"""
Audio Graph Snapshot — 不可变音频图快照（§1.1/1.4 目标架构）。

计划表 §5.1 的目标架构核心：AudioGraphSnapshot（不可变模型快照）+
CompiledAudioGraph（线程私有运行态）。当前实现采用渐进式：

- AudioGraphSnapshot：从 TimelineSequence 一次性构建的不可变快照，
  含所有声音字段（clip/track/bus/master）。禁止播放线程读取 UI 对象。
- revision：每次 timeline 变更生成带 revision 的 snapshot；可比较新旧快照
  判断是否需要重建运行态。
- stable hash：snapshot 可序列化且 hash 包含所有声音字段（供 §1.5 同步策略）。

设计原则（§1.1）：
1. UI/命令只修改 TimelineSequence，不直接改运行状态。
2. 每次 timeline 变更生成 snapshot；播放线程在 block 边界换图。
3. 图编译阶段验证未知 bus/重复 ID/cycle/无效 channel mapping/不支持插件；
   错误送到 UI/日志，不允许无声跳过。

注意：本模块是向 CompiledAudioGraph 演进的中间步骤。当前实时路径仍用
AudioMixer.mix_samples（签名同步触发重建），AudioGraphSnapshot 提供类型化
快照供未来原子换图使用，且复用 validate_routing_graph 做编译期校验。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple, TYPE_CHECKING

from src.features.edit.core.audio_edit import steps_signature

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import TimelineSequence


# ─────────────────────────────────────────────────────────────────────────────
# AudioGraphSnapshot：不可变音频图快照
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClipNodeSnapshot:
    """clip 节点快照（source/speed/fade/clip FX）。"""
    clip_id: str
    asset_id: str
    timeline_start: int
    timeline_duration: int
    source_start: int
    speed: float
    fade_in_frames: int
    fade_out_frames: int
    # B16：淡化曲线形状（None=音频 legacy equal_power）。R09：两端
    # 独立曲线（生效值——共享字段变化同样触发重建）。
    fade_curve: Optional[str] = None
    fade_curve_in: Optional[str] = None
    fade_curve_out: Optional[str] = None
    # B01: clip 增益（dB，听感字段；审计反例 clip_gain_control——此前
    # 快照完全缺该字段，改增益不触发重建）
    audio_gain_db: float = 0.0
    # 快照超集补齐（对照 edit_page 签名复审）：is_enabled（离线导出跳过
    # 禁用 clip）、采样域淡化（覆盖帧域值）、子帧放置偏移（F3.3）——
    # 任何一项变化都必须改变 stable_hash。
    is_enabled: bool = True
    fade_in_samples: Optional[int] = None
    fade_out_samples: Optional[int] = None
    audio_sample_offset: int = 0
    source_sample_offset: int = 0
    # A2：冻结 clip 注册时被跳过（无声化）——冻结状态是听感字段。
    freeze_source_frame: Optional[int] = None
    effects_signature: Tuple = ()  # clip FX 摘要
    # P0-3：Clip Editor 处理栈摘要（enabled 步骤的有序指纹；变化必须重建）。
    audio_edit_signature: Tuple = ()


@dataclass(frozen=True)
class TrackNodeSnapshot:
    """track/bus 节点快照（gain/pan/automation/insert FX/routing）。"""
    track_id: str
    name: str
    is_bus: bool
    volume: float
    pan: float
    is_muted: bool
    is_solo: bool
    is_enabled: bool
    output_bus_id: Optional[str]
    # B01: (target_bus_id, level, tap, muted)——tap 决定取样点、muted 独立
    # 断开 send 贡献；二者此前遗漏（审计反例 send_tap/send_muted）。
    sends: Tuple[Tuple[str, float, str, bool], ...]
    strip_eq_signature: Optional[str]
    effect_chain_signature: Optional[str]
    automation_signature: Optional[str]
    automation_mode: str
    # B01/P1-5: volume trim 倍率（离线渲染消费；审计反例 automation_trim）
    automation_trim: Tuple[Tuple[str, float], ...] = ()
    stem_group: str = ""
    # 快照超集补齐（对照 edit_page 签名复审）：空间声像器、audio:* 转场、
    # Bus 声道布局（引擎 set_bus_layouts 消费）都是听感字段。
    panner_signature: Optional[str] = None
    audio_transitions_signature: Tuple = ()
    bus_layout: str = "stereo"


@dataclass(frozen=True)
class MasterNodeSnapshot:
    """master 节点快照（meter/limiter/delivery）。"""
    volume: float
    muted: bool
    automation_signature: Optional[str]
    automation_mode: str
    # MX-03: Master 条带（EQ/FX 链）——edit_page 签名已含，快照超集补齐。
    strip_eq_signature: Optional[str] = None
    effect_chain_signature: Optional[str] = None


@dataclass(frozen=True)
class AudioGraphSnapshot:
    """不可变音频图快照（§1.1）。

    从 TimelineSequence 一次性构建，含所有声音字段。
    revision 单调递增；stable_hash 覆盖所有声音字段供同步判断。
    """
    revision: int
    fps: float
    sample_rate: int
    clips: Tuple[ClipNodeSnapshot, ...]
    tracks: Tuple[TrackNodeSnapshot, ...]
    master: MasterNodeSnapshot
    # B01: VCA 组快照（(vca_id, gain_db, muted, member_track_ids) 稳定序）。
    # VCA 增益/静音/成员变化改变实时听感（审计反例 vca_gain）。
    vcas: Tuple[Tuple[str, float, bool, Tuple[str, ...]], ...] = ()
    # 编译期校验结果
    validation_errors: Tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        """编译期校验通过（无环/无未知目标）。"""
        return len(self.validation_errors) == 0

    @property
    def stable_hash(self) -> str:
        """覆盖所有声音字段的稳定 hash（§1.5 同步判断用）。

        不含 revision（revision 是版本号，非声音字段；同 sequence 不同 revision
        的 hash 应相同，否则 needs_rebuild 永远为 True）。

        RMA-02/B1: 字段覆盖必须严格超集于 edit_page._build_audio_timeline_signature，
        否则用 snapshot 替代 signature 会丢重建触发。已补齐 clip 的 asset_id/source_start
        与 track 的 is_enabled（这三项此前漏算，会导致换素材/改源起点/禁用轨不触发重建）。
        """
        data = {
            "fps": round(self.fps, 6),
            "sr": self.sample_rate,
            # clip: asset_id（换素材）、source_start（改源起点）都会改变声音
            "c": [(c.clip_id, c.asset_id, c.source_start, c.timeline_start,
                   c.timeline_duration, c.speed, c.fade_in_frames, c.fade_out_frames,
                   c.fade_curve, c.fade_curve_in, c.fade_curve_out, c.audio_gain_db,
                   c.is_enabled, c.fade_in_samples, c.fade_out_samples,
                   c.audio_sample_offset, c.source_sample_offset,
                   c.freeze_source_frame,
                   c.effects_signature, c.audio_edit_signature) for c in self.clips],
            # track: is_enabled（禁用轨不发声）；B01: sends 含 tap/muted、
            # automation_trim 参与声音身份
            "t": [(t.track_id, t.volume, t.pan, t.is_muted, t.is_solo, t.is_enabled,
                   t.output_bus_id, t.sends, t.strip_eq_signature,
                   t.effect_chain_signature, t.automation_signature, t.automation_mode,
                   t.automation_trim, t.stem_group, t.is_bus,
                   t.panner_signature, t.audio_transitions_signature,
                   t.bus_layout) for t in self.tracks],
            "m": (self.master.volume, self.master.muted,
                  self.master.automation_signature, self.master.automation_mode,
                  self.master.strip_eq_signature, self.master.effect_chain_signature),
            # B01: VCA 组（gain/mute/成员）
            "v": self.vcas,
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

    def diff(self, other: "AudioGraphSnapshot") -> List[str]:
        """与另一 snapshot 的差异（字段级，供调试/日志）。"""
        diffs: List[str] = []
        if self.fps != other.fps:
            diffs.append(f"fps: {self.fps} → {other.fps}")
        if self.sample_rate != other.sample_rate:
            diffs.append(f"sample_rate: {self.sample_rate} → {other.sample_rate}")
        if self.master != other.master:
            diffs.append(f"master: {self.master} → {other.master}")
        # track diff
        self_tracks = {t.track_id: t for t in self.tracks}
        other_tracks = {t.track_id: t for t in other.tracks}
        for tid in set(self_tracks) | set(other_tracks):
            if tid not in other_tracks:
                diffs.append(f"track removed: {tid}")
            elif tid not in self_tracks:
                diffs.append(f"track added: {tid}")
            elif self_tracks[tid] != other_tracks[tid]:
                diffs.append(f"track changed: {tid}")
        return diffs


# ─────────────────────────────────────────────────────────────────────────────
# 快照构建 + 编译期校验
# ─────────────────────────────────────────────────────────────────────────────

def _clip_effects_signature(clip: Any) -> Tuple:
    """clip FX 摘要（effect_type + enabled + params hash）。"""
    effects = getattr(clip, "effects", ()) or ()
    sig = []
    for ef in effects:
        etype = getattr(ef, "effect_type", "")
        enabled = bool(getattr(ef, "is_enabled", True))
        params = getattr(ef, "params", None) or {}
        sig.append((etype, enabled, repr(sorted(params.items()) if params else [])))
    return tuple(sig)


def _strip_eq_signature(eq: Any) -> Optional[str]:
    if eq is None:
        return None
    if hasattr(eq, "to_dict"):
        return repr(sorted(eq.to_dict().items()))
    return repr(eq)


def _effect_chain_signature(chain: Any) -> Optional[str]:
    if chain is None:
        return None
    if hasattr(chain, "to_dict"):
        return repr(sorted(chain.to_dict().items()))
    return repr(chain)


def _repr_signature(obj: Any) -> Optional[str]:
    """通用对象签名（None=None；to_dict 稳定排序；否则 repr）。"""
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        return repr(sorted(obj.to_dict().items()))
    return repr(obj)


def _audio_transitions_signature(track: Any) -> Tuple:
    """轨内 audio:* 转场签名（crossfade 参数；视频转场不影响声音）。"""
    entries = []
    for tr in (getattr(track, "transitions", ()) or ()):
        ttype = str(getattr(tr, "transition_type", ""))
        if not ttype.startswith("audio:"):
            continue
        entries.append((
            ttype,
            getattr(tr, "transition_id", ""),
            getattr(tr, "left_clip_id", ""),
            getattr(tr, "right_clip_id", ""),
            int(getattr(tr, "center_frame", 0) or 0),
            int(getattr(tr, "duration", 0) or 0),
            str(getattr(tr, "alignment", "centered") or "centered"),
            repr(sorted((getattr(tr, "params", None) or {}).items())),
        ))
    return tuple(entries)


def _automation_signature(auto: Any) -> Optional[str]:
    if not auto:
        return None
    parts = []
    for param, track in auto.items():
        kfs = getattr(track, "keyframes", []) or []
        parts.append((param, [(kf.frame, kf.value) for kf in kfs]))
    return repr(sorted(parts))


def build_audio_graph_snapshot(
    sequence: "TimelineSequence",
    revision: int = 0,
) -> AudioGraphSnapshot:
    """从 TimelineSequence 构建不可变快照（§1.1）。

    一次性读取所有声音字段，禁止播放线程后续读取 UI 对象。
    含编译期校验（环/未知目标）。

    Args:
        sequence: TimelineSequence
        revision: 快照版本号（单调递增）

    Returns:
        AudioGraphSnapshot（含 validation_errors）
    """
    # clip 节点
    clips: List[ClipNodeSnapshot] = []
    for track in getattr(sequence, "audio_tracks", ()) or ():
        if getattr(track, "is_bus", False):
            continue
        for clip in getattr(track, "clips", ()) or ():
            speed = getattr(clip, "speed", 1.0)
            if hasattr(speed, "speed"):
                speed = speed.speed
            clips.append(ClipNodeSnapshot(
                clip_id=getattr(clip, "clip_id", ""),
                asset_id=getattr(clip, "asset_id", ""),
                timeline_start=getattr(clip, "timeline_start", 0),
                timeline_duration=getattr(clip, "timeline_duration", 0),
                source_start=getattr(clip, "source_start", 0),
                speed=float(speed),
                fade_in_frames=int(getattr(clip, "fade_in_duration", 0) or 0),
                fade_out_frames=int(getattr(clip, "fade_out_duration", 0) or 0),
                fade_curve=getattr(clip, "fade_curve", None),
                fade_curve_in=getattr(clip, "effective_fade_curve_in", None),
                fade_curve_out=getattr(clip, "effective_fade_curve_out", None),
                audio_gain_db=float(getattr(clip, "audio_gain_db", 0.0) or 0.0),
                is_enabled=bool(getattr(clip, "is_enabled", True)),
                fade_in_samples=getattr(clip, "fade_in_samples", None),
                fade_out_samples=getattr(clip, "fade_out_samples", None),
                audio_sample_offset=int(getattr(clip, "audio_sample_offset", 0) or 0),
                source_sample_offset=int(getattr(clip, "source_sample_offset", 0) or 0),
                freeze_source_frame=getattr(clip, "freeze_source_frame", None),
                effects_signature=_clip_effects_signature(clip),
                audio_edit_signature=steps_signature(
                    getattr(clip, "audio_edit", None)),
            ))

    # track/bus 节点
    tracks: List[TrackNodeSnapshot] = []
    for track in getattr(sequence, "audio_tracks", ()) or ():
        # B01: send 4 元组带 tap/muted（MX-14 模型 v2 字段入声音身份）
        sends = tuple(
            (getattr(s, "target_bus_id", ""), float(getattr(s, "level", 0.0)),
             str(getattr(s, "tap", "post_fader") or "post_fader"),
             bool(getattr(s, "muted", False)))
            for s in (getattr(track, "sends", ()) or ())
            if getattr(s, "enabled", True)
        )
        trim = getattr(track, "automation_trim", {}) or {}
        tracks.append(TrackNodeSnapshot(
            track_id=getattr(track, "track_id", ""),
            name=getattr(track, "name", ""),
            is_bus=bool(getattr(track, "is_bus", False)),
            volume=float(getattr(track, "volume", 1.0)),
            pan=float(getattr(track, "pan", 0.0)),
            is_muted=bool(getattr(track, "is_muted", False)),
            is_solo=bool(getattr(track, "is_solo", False)),
            is_enabled=bool(getattr(track, "is_enabled", True)),
            output_bus_id=getattr(track, "output_bus_id", None),
            sends=sends,
            strip_eq_signature=_strip_eq_signature(getattr(track, "strip_eq", None)),
            effect_chain_signature=_effect_chain_signature(getattr(track, "effect_chain", None)),
            automation_signature=_automation_signature(getattr(track, "automation", {})),
            automation_mode=str(getattr(track, "automation_mode", "off") or "off"),
            automation_trim=tuple(sorted(
                (str(k), float(v)) for k, v in trim.items())),
            stem_group=str(getattr(track, "stem_group", "") or ""),
            panner_signature=_repr_signature(getattr(track, "panner", None)),
            audio_transitions_signature=_audio_transitions_signature(track),
            bus_layout=str(getattr(track, "bus_layout", "stereo")
                           or "stereo"),
        ))

    # master 节点
    master = MasterNodeSnapshot(
        volume=float(getattr(sequence, "master_volume", 1.0)),
        muted=bool(getattr(sequence, "master_muted", False)),
        automation_signature=_automation_signature(getattr(sequence, "master_automation", {})),
        automation_mode=str(getattr(sequence, "master_automation_mode", "off") or "off"),
        strip_eq_signature=_strip_eq_signature(getattr(sequence, "master_strip_eq", None)),
        effect_chain_signature=_effect_chain_signature(
            getattr(sequence, "master_effect_chain", None)),
    )

    # 编译期校验（环/未知目标）
    validation_errors = _validate_snapshot(tracks)

    fps = getattr(sequence, "fps", 24.0)
    if hasattr(fps, "num"):
        fps = float(fps.num) / float(fps.den) if fps.den else 24.0

    # B01: VCA 组快照（稳定序，成员轨 ID 排序）
    vcas = tuple(sorted(
        (
            str(getattr(v, "vca_id", "")),
            float(getattr(v, "gain_db", 0.0)),
            bool(getattr(v, "muted", False)),
            tuple(sorted(str(m) for m in getattr(v, "member_track_ids", ()) or ())),
        )
        for v in (getattr(sequence, "vcas", ()) or ())
    ))

    return AudioGraphSnapshot(
        revision=revision,
        fps=fps,
        sample_rate=int(getattr(sequence, "sample_rate", 48000)),
        clips=tuple(clips),
        tracks=tuple(tracks),
        master=master,
        vcas=vcas,
        validation_errors=tuple(validation_errors),
    )


def _validate_snapshot(tracks: List[TrackNodeSnapshot]) -> List[str]:
    """编译期校验：环检测 + 未知目标 + 重复 ID（§1.1 要求）。"""
    errors: List[str] = []

    # 重复 ID
    seen_ids = set()
    for t in tracks:
        if t.track_id in seen_ids:
            errors.append(f"重复 track_id: {t.track_id}")
        seen_ids.add(t.track_id)

    # 环 + 未知目标（复用 validate_routing_graph）
    bus_ids = [t.track_id for t in tracks if t.is_bus]
    routing = {
        t.track_id: {
            "output_bus_id": t.output_bus_id,
            "sends": list(t.sends),
            "is_bus": t.is_bus,
        }
        for t in tracks
    }
    try:
        from src.features.edit.core.audio_playback import validate_routing_graph
        is_valid, routing_errors = validate_routing_graph(bus_ids, routing)
        errors.extend(routing_errors)
    except Exception as e:
        errors.append(f"路由校验失败: {e}")

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# SnapshotHistory：revision 管理（§1.4 原子换图的基础）
# ─────────────────────────────────────────────────────────────────────────────

class SnapshotHistory:
    """快照历史（§1.4 原子换图基础）。

    维护 revision 单调递增的快照序列，供播放线程在 block 边界原子换图。
    当前实现是轻量包装（实时路径仍用签名同步），未来 CompiledAudioGraph
    可基于此在 block 边界原子切换运行态。

    线程安全（AUD-15410）：
    - ``update()`` 与 ``current``/``revision`` 读取由内部锁保护
      （``_revision += 1`` 读-改-写此前非原子，跨线程 update/读取存在竞态）。
    - 跨线程消费契约：播放线程**不得**经 ``needs_rebuild(sequence)`` 重新遍历
      live TimelineSequence（违反模块头"禁止播放线程读取 UI 对象"原则——UI
      并发增删 clip/track 会漏项/错项或 "dict changed size during iteration"）。
      应改用 ``needs_rebuild_from(candidate)`` 比较 UI 线程预构建的不可变
      candidate，或直接消费 ``update()`` 发布的 ``current``。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[AudioGraphSnapshot] = None
        self._revision: int = 0

    @property
    def current(self) -> Optional[AudioGraphSnapshot]:
        with self._lock:
            return self._current

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def update(self, sequence: "TimelineSequence") -> AudioGraphSnapshot:
        """从 sequence 构建新快照，revision 递增。

        应在 timeline 变更后由 UI/模型线程调用：构建过程遍历 live 模型，
        与模型写线程同线程串行化才能避免并发读写。

        Returns:
            新的 AudioGraphSnapshot
        """
        with self._lock:
            self._revision += 1
            revision = self._revision
        snapshot = build_audio_graph_snapshot(sequence, revision)
        with self._lock:
            self._current = snapshot
        return snapshot

    def needs_rebuild(self, sequence: "TimelineSequence") -> bool:
        """判断是否需要重建（基于 stable_hash 比较）。

        兼容保留的便捷入口：在**调用方线程**遍历 live sequence 构建 candidate
        并比较 stable_hash（revision 不参与 hash，见 AudioGraphSnapshot.stable_hash）。
        勿在播放线程使用——见类 docstring 的跨线程消费契约（AUD-15410），
        跨线程场景改用 needs_rebuild_from()。
        """
        with self._lock:
            current = self._current
            revision = self._revision
        candidate = build_audio_graph_snapshot(sequence, revision)
        if current is None:
            return True
        return candidate.stable_hash != current.stable_hash

    def needs_rebuild_from(self, candidate: AudioGraphSnapshot) -> bool:
        """AUD-15410: 用预构建的不可变 candidate 判断是否需要重建。

        播放线程在 block 边界的安全入口：不触碰 live 模型，只比较两个不可变
        快照的 stable_hash。``_current`` 在锁内一次性取本地引用，避免 None
        判断与读取之间的 TOCTOU。
        """
        with self._lock:
            current = self._current
        if current is None:
            return True
        return candidate.stable_hash != current.stable_hash
