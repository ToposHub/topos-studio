"""
Mixer Preset & Snapshot（W13/W14）。

通道预设（ChannelStripPreset）：单条 strip 的 volume + pan + mute + solo + strip_eq 组合，
跨项目存储在用户目录（~/.topos/mixer_presets.json），可一键应用到任意轨道。

混音快照（MixSnapshot）：整个混音器所有 strip 状态的冻结副本，存到 timeline
（mixer_snapshots 字段），用于 A/B 对比。

线程安全：纯数据类，无并发访问。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Any
import json
import logging
import os
import uuid

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 通道预设（单条 strip 状态）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChannelStripPreset:
    """单条混音器 strip 的参数快照（W13）。

    包含一条 strip 的全部可调参数，可保存/加载/应用到任意轨道。
    存储位置：~/.topos/mixer_presets.json（用户级，跨项目）。

    Attributes:
        preset_id: 唯一标识
        name: 预设名称（用户可见）
        volume: 线性增益（0.0-2.0）
        pan: 声像（-1.0..1.0）
        is_muted: 静音状态
        is_solo: 独奏状态
        strip_eq: Strip EQ 参数（StripEQParams | None）
        effect_chain: FX 机架链（EffectChainParams | None）— AM-13 补齐
        output_bus_id: 输出目标 Bus（None=Master）— AM-13 补齐 routing
        sends: 发送列表 tuple[Send, ...] — AM-13 补齐 routing
        is_bus: 是否 Bus 轨 — AM-13 补齐 routing
        created_at: 创建时间戳（ISO 字符串）
    """

    preset_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Untitled Preset"
    volume: float = 1.0
    pan: float = 0.0
    is_muted: bool = False
    is_solo: bool = False
    strip_eq: Optional[Any] = None  # StripEQParams | None
    # AM-13: 补齐 effect_chain 与 routing，使预设覆盖完整 strip 状态
    effect_chain: Optional[Any] = None  # EffectChainParams | None
    output_bus_id: Optional[str] = None
    sends: tuple = ()  # tuple[Send, ...]
    is_bus: bool = False
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        # strip_eq / effect_chain 延迟序列化（避免 import 循环）
        se = None
        if self.strip_eq is not None and hasattr(self.strip_eq, 'to_dict'):
            se = self.strip_eq.to_dict()
        ec = None
        if self.effect_chain is not None and hasattr(self.effect_chain, 'to_dict'):
            ec = self.effect_chain.to_dict()
        return {
            "preset_id": self.preset_id,
            "name": self.name,
            "volume": self.volume,
            "pan": self.pan,
            "is_muted": self.is_muted,
            "is_solo": self.is_solo,
            "strip_eq": se,
            "effect_chain": ec,
            "output_bus_id": self.output_bus_id,
            "sends": [s.to_dict() if hasattr(s, 'to_dict') else s for s in self.sends],
            "is_bus": self.is_bus,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ChannelStripPreset":
        # strip_eq / effect_chain 延迟反序列化
        strip_eq = None
        se_data = data.get("strip_eq")
        if se_data:
            try:
                from src.features.edit.core.timeline_v2 import StripEQParams
                strip_eq = StripEQParams.from_dict(se_data)
            except Exception:
                strip_eq = None
        effect_chain = None
        ec_data = data.get("effect_chain")
        if ec_data:
            try:
                from src.features.edit.core.timeline_v2 import EffectChainParams
                effect_chain = EffectChainParams.from_dict(ec_data)
            except Exception:
                effect_chain = None
        # sends 反序列化
        sends = ()
        sends_data = data.get("sends") or []
        if sends_data:
            try:
                from src.features.edit.core.timeline_v2 import Send
                sends = tuple(
                    Send.from_dict(s) if isinstance(s, dict) else s
                    for s in sends_data
                )
            except Exception:
                sends = ()
        return ChannelStripPreset(
            preset_id=data.get("preset_id", str(uuid.uuid4())),
            name=data.get("name", "Untitled Preset"),
            volume=float(data.get("volume", 1.0)),
            pan=float(data.get("pan", 0.0)),
            is_muted=bool(data.get("is_muted", False)),
            is_solo=bool(data.get("is_solo", False)),
            strip_eq=strip_eq,
            effect_chain=effect_chain,
            output_bus_id=data.get("output_bus_id"),
            sends=sends,
            is_bus=bool(data.get("is_bus", False)),
            created_at=data.get("created_at", ""),
        )

    @staticmethod
    def from_track(track: Any, name: str = "") -> "ChannelStripPreset":
        """从 EditTrack 构造预设（提取其完整混音参数，含 FX 链与 routing）。"""
        from datetime import datetime
        return ChannelStripPreset(
            name=name or getattr(track, 'name', 'Preset'),
            volume=float(getattr(track, 'volume', 1.0)),
            pan=float(getattr(track, 'pan', 0.0)),
            is_muted=bool(getattr(track, 'is_muted', False)),
            is_solo=bool(getattr(track, 'is_solo', False)),
            strip_eq=getattr(track, 'strip_eq', None),
            # AM-13: 补齐 effect_chain 与 routing
            effect_chain=getattr(track, 'effect_chain', None),
            output_bus_id=getattr(track, 'output_bus_id', None),
            sends=tuple(getattr(track, 'sends', ()) or ()),
            is_bus=bool(getattr(track, 'is_bus', False)),
            created_at=datetime.now().isoformat(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 预设存储（用户级 JSON 文件）
# ─────────────────────────────────────────────────────────────────────────────

def _presets_path() -> str:
    """返回用户级预设文件路径（~/.topos/mixer_presets.json）。"""
    home = os.path.expanduser("~")
    topos_dir = os.path.join(home, ".topos")
    return os.path.join(topos_dir, "mixer_presets.json")


def _parse_preset_entry(item: Any) -> ChannelStripPreset | None:
    """解析单条预设记录；损坏条目返回 None（跳过，不拖垮整个列表）。"""
    if not isinstance(item, dict):
        return None
    try:
        return ChannelStripPreset.from_dict(item)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        logger.warning(f"mixer preset: skip corrupted entry ({exc!r})")
        return None


def _load_presets_for_write() -> list[ChannelStripPreset] | None:
    """为写路径（add/remove/save）加载现有预设。

    AUD-16906：逐条解析并跳过损坏条目；文件存在但整体不可解析（JSON
    损坏/schema 畸形）时返回 None——写路径据此拒绝整文件覆写，把仅剩
    人工修复机会的用户预设库保留在盘上。
    """
    path = _presets_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error(f"mixer preset library unreadable, keep file intact: {exc!r}")
        return None
    if not isinstance(data, dict):
        logger.error("mixer preset library schema invalid (not an object), keep file intact")
        return None
    entries = data.get("presets", [])
    if not isinstance(entries, list):
        logger.error("mixer preset library schema invalid ('presets' not a list), keep file intact")
        return None
    presets: list[ChannelStripPreset] = []
    for item in entries:
        parsed = _parse_preset_entry(item)
        if parsed is not None:
            presets.append(parsed)
    return presets


def load_presets() -> list[ChannelStripPreset]:
    """加载所有用户级预设。

    文件不存在或整体不可读时返回空列表；单条损坏的预设被跳过（带
    warning），不再让一条坏记录使整个预设库解析失败（AUD-16906）。
    """
    presets = _load_presets_for_write()
    return [] if presets is None else presets


def save_presets(presets: list[ChannelStripPreset]) -> bool:
    """保存预设列表到用户目录（临时文件 + os.replace 原子替换）。

    AUD-16906：写入中途的失败（磁盘满/权限/不可序列化数据）不再留下
    截断的半成品文件——要么完整替换，要么保持原文件不变。
    """
    path = _presets_path()
    tmp_path = f"{path}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {"presets": [p.to_dict() for p in presets]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.error(f"save mixer presets failed: {exc!r}")
        try:
            os.remove(tmp_path)
        except OSError:
            logger.debug(f"leave-behind temp file {tmp_path} not removable")
        return False


def add_preset(preset: ChannelStripPreset) -> list[ChannelStripPreset]:
    """添加一个预设（若同 id 已存在则替换），返回更新后的列表。

    AUD-16906：库文件存在但整体不可读时拒绝落盘覆写（仅返回内存视图），
    避免用单条新预设永久清空整个用户预设库。
    """
    presets = _load_presets_for_write()
    if presets is None:
        logger.error("add_preset: preset library unreadable, skip persisting")
        return [preset]
    presets = [p for p in presets if p.preset_id != preset.preset_id]
    presets.append(preset)
    save_presets(presets)
    return presets


def remove_preset(preset_id: str) -> list[ChannelStripPreset]:
    """删除指定 id 的预设，返回更新后的列表。

    AUD-16906：库文件存在但整体不可读时拒绝落盘覆写（仅返回内存视图）。
    """
    presets = _load_presets_for_write()
    if presets is None:
        logger.error("remove_preset: preset library unreadable, skip persisting")
        return []
    presets = [p for p in presets if p.preset_id != preset_id]
    save_presets(presets)
    return presets


# ─────────────────────────────────────────────────────────────────────────────
# 混音快照（整个混音器状态，用于 A/B 对比）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MixSnapshot:
    """整个混音器的状态快照（W14）。

    包含所有音频轨的 strip 参数 + Master 参数。存到 timeline 的
    mixer_snapshots 字段，用于 A/B 对比切换。

    Attributes:
        snapshot_id: 唯一标识
        name: 快照名称（"A" / "B" / 自定义）
        track_states: {track_id: ChannelStripPreset-like dict}
        master_volume: Master 总线音量（0.0-1.0）
        master_muted: Master 静音
    """

    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Snapshot"
    track_states: dict[str, dict] = field(default_factory=dict)
    master_volume: float = 1.0
    master_muted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "name": self.name,
            "track_states": dict(self.track_states),
            "master_volume": self.master_volume,
            "master_muted": self.master_muted,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MixSnapshot":
        return MixSnapshot(
            snapshot_id=data.get("snapshot_id", str(uuid.uuid4())),
            name=data.get("name", "Snapshot"),
            track_states=dict(data.get("track_states", {})),
            master_volume=float(data.get("master_volume", 1.0)),
            master_muted=bool(data.get("master_muted", False)),
        )

    @staticmethod
    def from_timeline(timeline: Any, name: str = "Snapshot") -> "MixSnapshot":
        """从当前 timeline 构造快照（提取所有音频轨 + Master 状态）。"""
        track_states: dict[str, dict] = {}
        for track in getattr(timeline, 'audio_tracks', ()):
            preset = ChannelStripPreset.from_track(track)
            track_states[track.track_id] = preset.to_dict()
        return MixSnapshot(
            name=name,
            track_states=track_states,
            master_volume=float(getattr(timeline, 'master_volume', 1.0)),
            master_muted=bool(getattr(timeline, 'master_muted', False)),
        )
