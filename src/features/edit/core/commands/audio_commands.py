"""Audio mixer & automation timeline commands (P6 split, moved verbatim).

Extracted from commands/__init__.py: audio_mixer cluster (track volume/pan/
strip EQ/effect chain/master/Bus/Send routing) + automation cluster
(automation keyframes). Class sources are byte-identical to the original
module; to_dict/from_dict keys are a persistence contract — do not rename.
"""


from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional, Tuple
import logging
import uuid

from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SetTrackVolumeCommand:
    """
    Command to set track volume.

    Volume ranges from 0.0 (silent) to 2.0 (200% boost).
    Default is 1.0 (100%).

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID to set volume
        volume: New volume value (0.0-2.0)
        _old_volume: Old volume value (for undo)
    """

    command_id: str
    label: str
    track_id: str
    volume: float
    _old_volume: float | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command: set volume."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Clamp volume to valid range
        new_volume = max(0.0, min(2.0, self.volume))
        old_volume = track.volume

        # 用 with_volume 保持所有其他字段（含 strip_eq/automation 等新字段）
        new_track = track.with_volume(new_volume)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackVolumeCommand applied: track {self.track_id} volume={new_volume:.2f}")

        # Return updated command with stored old_volume for undo
        updated_cmd = SetTrackVolumeCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            volume=self.volume,
            _old_volume=old_volume,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old volume."""
        if self._old_volume is None:
            logger.warning("SetTrackVolumeCommand undo: old_volume not stored")
            return timeline

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        new_track = track.with_volume(self._old_volume)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackVolumeCommand undone: track {self.track_id} volume={self._old_volume:.2f}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackVolumeCommand",
            "label": self.label,
            "track_id": self.track_id,
            "volume": self.volume,
            "old_volume": self._old_volume,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetTrackVolumeCommand:
        return SetTrackVolumeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Track Volume"),
            track_id=data.get("track_id", ""),
            volume=data.get("volume", 1.0),
            _old_volume=data.get("old_volume"),
        )

    @staticmethod
    def create(track_id: str, volume: float) -> SetTrackVolumeCommand:
        return SetTrackVolumeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Track Volume: {volume:.0%}",
            track_id=track_id,
            volume=volume,
        )


@dataclass(frozen=True)
class SetTrackPanCommand:
    """
    Command to set track pan (stereo balance / 声相).

    Pan ranges from -1.0 (full left) through 0.0 (center) to 1.0 (full right).
    Default is 0.0 (center).

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID to set pan
        pan: New pan value (-1.0..1.0)
        _old_pan: Old pan value (for undo)
    """

    command_id: str
    label: str
    track_id: str
    pan: float
    _old_pan: float | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command: set pan."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Clamp pan to valid range
        new_pan = max(-1.0, min(1.0, self.pan))
        old_pan = track.pan

        new_track = track.with_pan(new_pan)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackPanCommand applied: track {self.track_id} pan={new_pan:+.2f}")

        # Return updated command with stored old_pan for undo
        updated_cmd = SetTrackPanCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            pan=self.pan,
            _old_pan=old_pan,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old pan."""
        if self._old_pan is None:
            logger.warning("SetTrackPanCommand undo: old_pan not stored")
            return timeline

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        new_track = track.with_pan(self._old_pan)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackPanCommand undone: track {self.track_id} pan={self._old_pan:+.2f}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackPanCommand",
            "label": self.label,
            "track_id": self.track_id,
            "pan": self.pan,
            "old_pan": self._old_pan,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetTrackPanCommand:
        return SetTrackPanCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Track Pan"),
            track_id=data.get("track_id", ""),
            pan=data.get("pan", 0.0),
            _old_pan=data.get("old_pan"),
        )

    @staticmethod
    def create(track_id: str, pan: float) -> SetTrackPanCommand:
        return SetTrackPanCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Track Pan: {pan:+.2f}",
            track_id=track_id,
            pan=pan,
        )


@dataclass(frozen=True)
class SetTrackStripEQCommand:
    """
    Command to set a track's Strip EQ parameters (W6).

    Strip EQ is a 3-band (lowshelf / peaking / highshelf) per-track equalizer
    that is part of the professional mixer channel strip.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID to set Strip EQ
        strip_eq: New StripEQParams (or None to disable EQ)
        _old_strip_eq: Old Strip EQ params (for undo)
    """

    command_id: str
    label: str
    track_id: str
    strip_eq: Optional[Any]  # StripEQParams | None
    _old_strip_eq: Optional[Any] = None  # StripEQParams | None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "SetTrackStripEQCommand"]:
        """Apply the command: set Strip EQ."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        old_strip_eq = track.strip_eq
        new_track = track.with_strip_eq(self.strip_eq)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackStripEQCommand applied: track {self.track_id}")

        updated_cmd = SetTrackStripEQCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            strip_eq=self.strip_eq,
            _old_strip_eq=old_strip_eq,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old Strip EQ."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        new_track = track.with_strip_eq(self._old_strip_eq)
        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackStripEQCommand undone: track {self.track_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackStripEQCommand",
            "label": self.label,
            "track_id": self.track_id,
            "strip_eq": self.strip_eq.to_dict() if self.strip_eq is not None else None,
            "old_strip_eq": self._old_strip_eq.to_dict() if self._old_strip_eq is not None else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetTrackStripEQCommand":
        # 延迟导入避免循环依赖
        from src.features.edit.core.timeline_v2 import StripEQParams
        se = data.get("strip_eq")
        old = data.get("old_strip_eq")
        return SetTrackStripEQCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Track Strip EQ"),
            track_id=data.get("track_id", ""),
            strip_eq=StripEQParams.from_dict(se) if se else None,
            _old_strip_eq=StripEQParams.from_dict(old) if old else None,
        )

    @staticmethod
    def create(track_id: str, strip_eq: Optional[Any]) -> "SetTrackStripEQCommand":
        return SetTrackStripEQCommand(
            command_id=str(uuid.uuid4()),
            label="Set Track Strip EQ",
            track_id=track_id,
            strip_eq=strip_eq,
        )


@dataclass(frozen=True)
class SetTrackEffectChainCommand:
    """Command to set a track's FX rack effect chain (W26).

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID
        effect_chain: New EffectChainParams (or None to clear chain)
        _old_effect_chain: Old chain (for undo)
    """

    command_id: str
    label: str
    track_id: str
    effect_chain: Optional[Any]  # EffectChainParams | None
    _old_effect_chain: Optional[Any] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "SetTrackEffectChainCommand"]:
        """Apply the command: set effect chain."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        old_chain = track.effect_chain
        new_track = track.with_effect_chain(self.effect_chain)
        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackEffectChainCommand applied: track {self.track_id}")

        updated_cmd = SetTrackEffectChainCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            effect_chain=self.effect_chain,
            _old_effect_chain=old_chain,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old effect chain."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        new_track = track.with_effect_chain(self._old_effect_chain)
        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackEffectChainCommand undone: track {self.track_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackEffectChainCommand",
            "label": self.label,
            "track_id": self.track_id,
            "effect_chain": self.effect_chain.to_dict() if self.effect_chain is not None else None,
            "old_effect_chain": self._old_effect_chain.to_dict() if self._old_effect_chain is not None else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetTrackEffectChainCommand":
        from src.features.edit.core.timeline_v2 import EffectChainParams
        ec = data.get("effect_chain")
        old = data.get("old_effect_chain")
        return SetTrackEffectChainCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Track FX Chain"),
            track_id=data.get("track_id", ""),
            effect_chain=EffectChainParams.from_dict(ec) if ec else None,
            _old_effect_chain=EffectChainParams.from_dict(old) if old else None,
        )

    @staticmethod
    def create(track_id: str, effect_chain: Optional[Any]) -> "SetTrackEffectChainCommand":
        return SetTrackEffectChainCommand(
            command_id=str(uuid.uuid4()),
            label="Set Track FX Chain",
            track_id=track_id,
            effect_chain=effect_chain,
        )


@dataclass(frozen=True)
class SetMasterVolumeCommand:
    """
    Command to set Master bus volume (W12).

    Master volume is the final output gain (linear, 0.0-2.0) applied after all track
    mixing. Unlike track volume, it does NOT enter the per-track signal path — it scales
    the final master output in AudioMixer.mix_samples (after summation, before soft-limit).
    Range 0.0-2.0 supports >0dB positive gain (e.g. +6dB ≈ 2.0); the soft-limiter in
    mix_samples protects against clipping. Persisted to the project file so it survives reload.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        volume: New master volume (linear, 0.0-2.0)
        _old_volume: Old master volume (for undo)
    """

    command_id: str
    label: str
    volume: float
    _old_volume: float | None = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "SetMasterVolumeCommand"]:
        """Apply the command: set Master volume."""
        new_volume = max(0.0, min(2.0, self.volume))
        old_volume = timeline.master_volume
        new_timeline = timeline.with_master_volume(new_volume)
        logger.debug(f"SetMasterVolumeCommand applied: volume={new_volume:.2f}")
        updated_cmd = SetMasterVolumeCommand(
            command_id=self.command_id,
            label=self.label,
            volume=self.volume,
            _old_volume=old_volume,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old Master volume."""
        if self._old_volume is None:
            logger.warning("SetMasterVolumeCommand undo: old_volume not stored")
            return timeline
        return timeline.with_master_volume(self._old_volume)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetMasterVolumeCommand",
            "label": self.label,
            "volume": self.volume,
            "old_volume": self._old_volume,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetMasterVolumeCommand":
        return SetMasterVolumeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Master Volume"),
            volume=float(data.get("volume", 1.0)),
            _old_volume=data.get("old_volume"),
        )

    @staticmethod
    def create(volume: float) -> "SetMasterVolumeCommand":
        return SetMasterVolumeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Master Volume: {volume:.0%}",
            volume=volume,
        )


@dataclass(frozen=True)
class SetMasterMuteCommand:
    """
    Command to set Master bus mute state (W12).

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        muted: New mute state
        _old_muted: Old mute state (for undo)
    """

    command_id: str
    label: str
    muted: bool
    _old_muted: bool | None = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "SetMasterMuteCommand"]:
        """Apply the command: set Master mute."""
        old_muted = timeline.master_muted
        new_timeline = timeline.with_master_muted(self.muted)
        logger.debug(f"SetMasterMuteCommand applied: muted={self.muted}")
        updated_cmd = SetMasterMuteCommand(
            command_id=self.command_id,
            label=self.label,
            muted=self.muted,
            _old_muted=old_muted,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old Master mute."""
        if self._old_muted is None:
            logger.warning("SetMasterMuteCommand undo: old_muted not stored")
            return timeline
        return timeline.with_master_muted(self._old_muted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetMasterMuteCommand",
            "label": self.label,
            "muted": self.muted,
            "old_muted": self._old_muted,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetMasterMuteCommand":
        return SetMasterMuteCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Master Mute"),
            muted=bool(data.get("muted", False)),
            _old_muted=data.get("old_muted"),
        )

    @staticmethod
    def create(muted: bool) -> "SetMasterMuteCommand":
        return SetMasterMuteCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Master Mute: {'On' if muted else 'Off'}",
            muted=muted,
        )


@dataclass(frozen=True)
class ApplyChannelStripPresetCommand:
    """
    Apply a channel strip preset to a track (W13).

    把一条 strip 的 volume + pan + mute + solo + strip_eq 一次性应用到目标轨道，
    打包成单次 undo（而非多个独立命令）。

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Target track ID
        volume / pan / is_muted / is_solo / strip_eq: 新值
        _old_volume / _old_pan / _old_muted / _old_solo / _old_strip_eq: 旧值（undo 用）
    """

    command_id: str
    label: str
    track_id: str
    volume: float
    pan: float
    is_muted: bool
    is_solo: bool
    strip_eq: Optional[Any] = None
    # 旧值（apply 时填充）
    _old_volume: float | None = None
    _old_pan: float | None = None
    _old_muted: bool | None = None
    _old_solo: bool | None = None
    _old_strip_eq: Optional[Any] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "ApplyChannelStripPresetCommand"]:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # 记录旧值
        old = (track.volume, track.pan, track.is_muted, track.is_solo, track.strip_eq)

        # 一次性应用所有参数（链式 with_*）
        new_track = track.with_volume(self.volume).with_pan(self.pan)
        new_track = new_track.with_muted(self.is_muted).with_solo(self.is_solo)
        new_track = new_track.with_strip_eq(self.strip_eq)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"ApplyChannelStripPresetCommand applied: track {self.track_id}")

        updated_cmd = ApplyChannelStripPresetCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            volume=self.volume, pan=self.pan,
            is_muted=self.is_muted, is_solo=self.is_solo, strip_eq=self.strip_eq,
            _old_volume=old[0], _old_pan=old[1], _old_muted=old[2],
            _old_solo=old[3], _old_strip_eq=old[4],
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        new_track = track.with_volume(self._old_volume).with_pan(self._old_pan)
        new_track = new_track.with_muted(self._old_muted).with_solo(self._old_solo)
        new_track = new_track.with_strip_eq(self._old_strip_eq)
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "ApplyChannelStripPresetCommand",
            "label": self.label,
            "track_id": self.track_id,
            "volume": self.volume, "pan": self.pan,
            "is_muted": self.is_muted, "is_solo": self.is_solo,
            "strip_eq": self.strip_eq.to_dict() if self.strip_eq is not None else None,
            "old_volume": self._old_volume, "old_pan": self._old_pan,
            "old_muted": self._old_muted, "old_solo": self._old_solo,
            "old_strip_eq": self._old_strip_eq.to_dict() if self._old_strip_eq is not None else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ApplyChannelStripPresetCommand":
        from src.features.edit.core.timeline_v2 import StripEQParams
        se = data.get("strip_eq")
        old_se = data.get("old_strip_eq")
        return ApplyChannelStripPresetCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Apply Strip Preset"),
            track_id=data.get("track_id", ""),
            volume=float(data.get("volume", 1.0)), pan=float(data.get("pan", 0.0)),
            is_muted=bool(data.get("is_muted", False)), is_solo=bool(data.get("is_solo", False)),
            strip_eq=StripEQParams.from_dict(se) if se else None,
            _old_volume=data.get("old_volume"), _old_pan=data.get("old_pan"),
            _old_muted=data.get("old_muted"), _old_solo=data.get("old_solo"),
            _old_strip_eq=StripEQParams.from_dict(old_se) if old_se else None,
        )

    @staticmethod
    def create_from_preset(track_id: str, preset: Any) -> "ApplyChannelStripPresetCommand":
        """从 ChannelStripPreset 构造命令。"""
        return ApplyChannelStripPresetCommand(
            command_id=str(uuid.uuid4()),
            label=f"Apply Preset: {preset.name}",
            track_id=track_id,
            volume=preset.volume, pan=preset.pan,
            is_muted=preset.is_muted, is_solo=preset.is_solo,
            strip_eq=preset.strip_eq,
        )


@dataclass(frozen=True)
class ApplyMixSnapshotCommand:
    """
    Apply a mix snapshot to the entire timeline (W14).

    把整个混音快照（所有音频轨 + Master）应用到 timeline，打包成单次 undo。
    用于 A/B 对比切换。

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        snapshot_name: 快照名称（"A" / "B"）
        track_states: {track_id: {volume, pan, is_muted, is_solo, strip_eq}}
        master_volume / master_muted: Master 状态
        _old_snapshot: 旧快照（undo 时恢复，dict 形式）
    """

    command_id: str
    label: str
    snapshot_name: str
    track_states: dict = field(default_factory=dict)
    master_volume: float = 1.0
    master_muted: bool = False
    _old_snapshot: Optional[dict] = None

    def _capture_current(self, timeline: TimelineSequence) -> dict:
        """捕获当前 timeline 的混音状态为 dict（用于 undo）。"""
        track_states: dict[str, dict] = {}
        for track in timeline.audio_tracks:
            track_states[track.track_id] = {
                "volume": track.volume, "pan": track.pan,
                "is_muted": track.is_muted, "is_solo": track.is_solo,
                "strip_eq": track.strip_eq.to_dict() if track.strip_eq is not None else None,
            }
        return {
            "track_states": track_states,
            "master_volume": timeline.master_volume,
            "master_muted": timeline.master_muted,
        }

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "ApplyMixSnapshotCommand"]:
        old_snapshot = self._capture_current(timeline)
        new_timeline = timeline
        # 应用每条轨道
        for track in new_timeline.audio_tracks:
            ts = self.track_states.get(track.track_id)
            if ts is None:
                continue
            new_track = track.with_volume(float(ts.get("volume", 1.0)))
            new_track = new_track.with_pan(float(ts.get("pan", 0.0)))
            new_track = new_track.with_muted(bool(ts.get("is_muted", False)))
            new_track = new_track.with_solo(bool(ts.get("is_solo", False)))
            # strip_eq
            se = ts.get("strip_eq")
            if se:
                try:
                    from src.features.edit.core.timeline_v2 import StripEQParams
                    new_track = new_track.with_strip_eq(StripEQParams.from_dict(se))
                except Exception as e:
                    logger.warning(
                        f"ApplyMixSnapshotCommand: strip EQ parse failed "
                        f"for track {track.track_id}: {e}")
            else:
                new_track = new_track.with_strip_eq(None)
            new_timeline = new_timeline.with_track_updated(new_track)
        # Master
        new_timeline = new_timeline.with_master_volume(self.master_volume)
        new_timeline = new_timeline.with_master_muted(self.master_muted)

        updated_cmd = ApplyMixSnapshotCommand(
            command_id=self.command_id, label=self.label,
            snapshot_name=self.snapshot_name, track_states=self.track_states,
            master_volume=self.master_volume, master_muted=self.master_muted,
            _old_snapshot=old_snapshot,
        )
        logger.debug(f"ApplyMixSnapshotCommand applied: {self.snapshot_name}")
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_snapshot is None:
            return timeline
        new_timeline = timeline
        for track in new_timeline.audio_tracks:
            ts = self._old_snapshot.get("track_states", {}).get(track.track_id)
            if ts is None:
                continue
            new_track = track.with_volume(float(ts.get("volume", 1.0)))
            new_track = new_track.with_pan(float(ts.get("pan", 0.0)))
            new_track = new_track.with_muted(bool(ts.get("is_muted", False)))
            new_track = new_track.with_solo(bool(ts.get("is_solo", False)))
            new_timeline = new_timeline.with_track_updated(new_track)
        new_timeline = new_timeline.with_master_volume(self._old_snapshot.get("master_volume", 1.0))
        new_timeline = new_timeline.with_master_muted(self._old_snapshot.get("master_muted", False))
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "ApplyMixSnapshotCommand",
            "label": self.label,
            "snapshot_name": self.snapshot_name,
            "track_states": self.track_states,
            "master_volume": self.master_volume,
            "master_muted": self.master_muted,
            "old_snapshot": self._old_snapshot,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ApplyMixSnapshotCommand":
        return ApplyMixSnapshotCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Apply Mix Snapshot"),
            snapshot_name=data.get("snapshot_name", "Snapshot"),
            track_states=data.get("track_states", {}),
            master_volume=float(data.get("master_volume", 1.0)),
            master_muted=bool(data.get("master_muted", False)),
            _old_snapshot=data.get("old_snapshot"),
        )

    @staticmethod
    def create_from_snapshot(snapshot: Any) -> "ApplyMixSnapshotCommand":
        """从 MixSnapshot 构造命令。"""
        return ApplyMixSnapshotCommand(
            command_id=str(uuid.uuid4()),
            label=f"Apply Snapshot: {snapshot.name}",
            snapshot_name=snapshot.name,
            track_states=dict(snapshot.track_states),
            master_volume=snapshot.master_volume,
            master_muted=snapshot.master_muted,
        )


# ── W20: Automation 关键帧命令（volume/pan/mute）──

@dataclass(frozen=True)
class AddAutomationKeyframeCommand:
    """Add an automation keyframe to a track parameter (W20).

    Attributes:
        command_id, label, track_id: 标准字段
        param: 参数名（"volume" / "pan" / "mute"）
        frame: 关键帧帧号
        value: 关键帧值（volume=0.0-2.0, pan=-1..1, mute=0/1）
        interpolation: 插值类型（"linear"/"bezier"/"hold"/...）
        min_val / max_val: 参数值域（供 KeyframeTrack 插值 clamp）
        _old_track_snapshot: 旧 automation dict 快照（undo 用，序列化为 dict-of-dict）
    """

    command_id: str
    label: str
    track_id: str
    param: str
    frame: int
    value: float
    interpolation: str = "linear"
    min_val: float = 0.0
    max_val: float = 1.0
    _old_track_snapshot: Optional[dict] = None

    def _snapshot(self, track: EditTrack) -> dict:
        return {k: v.to_dict() for k, v in track.automation.items()}

    def _restore(self, timeline: TimelineSequence, snapshot: dict) -> TimelineSequence:
        from src.shared.keyframe import KeyframeTrack
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        new_auto: dict = {}
        for param, td in snapshot.items():
            try:
                new_auto[param] = KeyframeTrack.from_dict(td)
            except Exception as e:
                logger.warning(
                    f"Automation restore: KeyframeTrack.from_dict failed "
                    f"for param {param}: {e}")
        new_track = track._rebuild(automation=new_auto)
        return timeline.with_track_updated(new_track)

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "AddAutomationKeyframeCommand"]:
        from src.shared.keyframe import Keyframe
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old_snap = self._snapshot(track)

        interp = SetAutomationKeyframeInterpolationCommand._resolve_interp(
            self.interpolation
        )
        kf = Keyframe(frame=self.frame, value=self.value, interpolation=interp)
        new_track = track.with_automation_keyframe_added(
            self.param, kf, self.min_val, self.max_val
        )
        new_timeline = timeline.with_track_updated(new_track)
        updated_cmd = AddAutomationKeyframeCommand(
            command_id=self.command_id, label=self.label, track_id=self.track_id,
            param=self.param, frame=self.frame, value=self.value,
            interpolation=self.interpolation, min_val=self.min_val, max_val=self.max_val,
            _old_track_snapshot=old_snap,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_track_snapshot is None:
            return timeline
        return self._restore(timeline, self._old_track_snapshot)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "AddAutomationKeyframeCommand",
            "label": self.label, "track_id": self.track_id,
            "param": self.param, "frame": self.frame, "value": self.value,
            "interpolation": self.interpolation, "min_val": self.min_val, "max_val": self.max_val,
            "old_track_snapshot": self._old_track_snapshot,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AddAutomationKeyframeCommand":
        return AddAutomationKeyframeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Add Automation Keyframe"),
            track_id=data.get("track_id", ""), param=data.get("param", "volume"),
            frame=int(data.get("frame", 0)), value=float(data.get("value", 1.0)),
            interpolation=data.get("interpolation", "linear"),
            min_val=float(data.get("min_val", 0.0)), max_val=float(data.get("max_val", 1.0)),
            _old_track_snapshot=data.get("old_track_snapshot"),
        )

    @staticmethod
    def create(track_id: str, param: str, frame: int, value: float,
               interpolation: str = "linear", min_val: float = 0.0, max_val: float = 1.0
               ) -> "AddAutomationKeyframeCommand":
        return AddAutomationKeyframeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add {param} Automation @ F{frame}",
            track_id=track_id, param=param, frame=frame, value=value,
            interpolation=interpolation, min_val=min_val, max_val=max_val,
        )


@dataclass(frozen=True)
class RemoveAutomationKeyframeCommand:
    """Remove an automation keyframe (W20)."""

    command_id: str
    label: str
    track_id: str
    param: str
    frame: int
    _old_track_snapshot: Optional[dict] = None
    # 被删除的关键帧值（undo 时重新添加）
    _removed_value: float = 0.0
    _removed_interpolation: str = "linear"
    _removed_min_val: float = 0.0
    _removed_max_val: float = 1.0

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "RemoveAutomationKeyframeCommand"]:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old_snap = {k: v.to_dict() for k, v in track.automation.items()}
        # 记录被删除的关键帧值（供 undo 重建）
        removed_val = self._removed_value
        removed_interp = self._removed_interpolation
        removed_min = self._removed_min_val
        removed_max = self._removed_max_val
        auto_track = track.automation.get(self.param)
        if auto_track is not None:
            kf = auto_track.get_keyframe_at(self.frame)
            if kf is not None:
                removed_val = kf.value
                removed_interp = kf.interpolation.name.lower() if hasattr(kf.interpolation, 'name') else "linear"
                removed_min = auto_track.min_val
                removed_max = auto_track.max_val
        new_track = track.with_automation_keyframe_removed(self.param, self.frame)
        new_timeline = timeline.with_track_updated(new_track)
        updated_cmd = RemoveAutomationKeyframeCommand(
            command_id=self.command_id, label=self.label, track_id=self.track_id,
            param=self.param, frame=self.frame, _old_track_snapshot=old_snap,
            _removed_value=removed_val, _removed_interpolation=removed_interp,
            _removed_min_val=removed_min, _removed_max_val=removed_max,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_track_snapshot is None:
            return timeline
        from src.shared.keyframe import KeyframeTrack
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        new_auto: dict = {}
        for param, td in self._old_track_snapshot.items():
            try:
                new_auto[param] = KeyframeTrack.from_dict(td)
            except Exception as e:
                logger.warning(
                    f"Automation undo: KeyframeTrack.from_dict failed "
                    f"for param {param}: {e}")
        new_track = track._rebuild(automation=new_auto)
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id, "command_type": "RemoveAutomationKeyframeCommand",
            "label": self.label, "track_id": self.track_id,
            "param": self.param, "frame": self.frame,
            "old_track_snapshot": self._old_track_snapshot,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RemoveAutomationKeyframeCommand":
        return RemoveAutomationKeyframeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Remove Automation Keyframe"),
            track_id=data.get("track_id", ""), param=data.get("param", "volume"),
            frame=int(data.get("frame", 0)),
            _old_track_snapshot=data.get("old_track_snapshot"),
        )

    @staticmethod
    def create(track_id: str, param: str, frame: int) -> "RemoveAutomationKeyframeCommand":
        return RemoveAutomationKeyframeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Remove {param} Automation @ F{frame}",
            track_id=track_id, param=param, frame=frame,
        )


@dataclass(frozen=True)
class ClearAutomationCommand:
    """Clear all automation keyframes for a parameter (W20)."""

    command_id: str
    label: str
    track_id: str
    param: str
    _old_track_snapshot: Optional[dict] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "ClearAutomationCommand"]:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old_snap = {k: v.to_dict() for k, v in track.automation.items()}
        new_track = track.with_automation_cleared(self.param)
        new_timeline = timeline.with_track_updated(new_track)
        updated_cmd = ClearAutomationCommand(
            command_id=self.command_id, label=self.label, track_id=self.track_id,
            param=self.param, _old_track_snapshot=old_snap,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_track_snapshot is None:
            return timeline
        from src.shared.keyframe import KeyframeTrack
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        new_auto: dict = {}
        for param, td in self._old_track_snapshot.items():
            try:
                new_auto[param] = KeyframeTrack.from_dict(td)
            except Exception as e:
                logger.warning(
                    f"Automation undo: KeyframeTrack.from_dict failed "
                    f"for param {param}: {e}")
        new_track = track._rebuild(automation=new_auto)
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id, "command_type": "ClearAutomationCommand",
            "label": self.label, "track_id": self.track_id,
            "param": self.param, "old_track_snapshot": self._old_track_snapshot,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ClearAutomationCommand":
        return ClearAutomationCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Clear Automation"),
            track_id=data.get("track_id", ""), param=data.get("param", "volume"),
            _old_track_snapshot=data.get("old_track_snapshot"),
        )

    @staticmethod
    def create(track_id: str, param: str) -> "ClearAutomationCommand":
        return ClearAutomationCommand(
            command_id=str(uuid.uuid4()),
            label=f"Clear {param} Automation",
            track_id=track_id, param=param,
        )


@dataclass(frozen=True)
class SetAutomationKeyframeInterpolationCommand:
    """Change the interpolation type of a track automation keyframe
    (AUD-37702, W20 消费端).

    Automation Lane 双击关键帧切换插值（linear/bezier/hold）时由
    edit_controller 提交。仅改插值类型；帧号、值、bezier/手柄等其余
    Keyframe 字段原样保留（基于 dataclasses.replace）。undo 走整个
    automation dict 快照恢复，与 Add/Remove/Clear Automation 命令同型。

    Attributes:
        command_id, label, track_id, param: 标准字段
        frame: 关键帧帧号
        interpolation: 新插值类型（InterpolationType 的 value/name，
            如 "linear" / "bezier" / "hold"）
        _old_track_snapshot: 旧 automation dict 快照（undo 用）
    """

    command_id: str
    label: str
    track_id: str
    param: str
    frame: int
    interpolation: str = "linear"
    _old_track_snapshot: Optional[dict] = None

    @staticmethod
    def _resolve_interp(name: str):
        """把插值名字符串解析为 InterpolationType 成员。

        先按枚举 value（"linear"/"bezier"/...）匹配，再按成员名（大小写
        不敏感）匹配；均未命中回退 LINEAR。注意不能直接
        ``InterpolationType(name.upper())`` —— 枚举按 value 查找，
        "LINEAR" 并非任何成员的 value。
        """
        from src.shared.keyframe import InterpolationType
        cleaned = str(name).strip().lower()
        for member in InterpolationType:
            if member.value == cleaned or member.name.lower() == cleaned:
                return member
        return InterpolationType.LINEAR

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "SetAutomationKeyframeInterpolationCommand"]:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        auto_track = track.automation.get(self.param)
        kf = auto_track.get_keyframe_at(self.frame) if auto_track is not None else None
        if kf is None:
            raise ValueError(
                f"No {self.param} automation keyframe at frame {self.frame}")
        old_snap = {k: v.to_dict() for k, v in track.automation.items()}
        # 保留 value/帧号/手柄，仅替换 interpolation
        new_kf = replace(kf, interpolation=self._resolve_interp(self.interpolation))
        new_track = track.with_automation_keyframe_updated(
            self.param, self.frame, new_kf)
        new_timeline = timeline.with_track_updated(new_track)
        updated_cmd = SetAutomationKeyframeInterpolationCommand(
            command_id=self.command_id, label=self.label, track_id=self.track_id,
            param=self.param, frame=self.frame, interpolation=self.interpolation,
            _old_track_snapshot=old_snap,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_track_snapshot is None:
            return timeline
        from src.shared.keyframe import KeyframeTrack
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        new_auto: dict = {}
        for param, td in self._old_track_snapshot.items():
            try:
                new_auto[param] = KeyframeTrack.from_dict(td)
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                logger.warning(
                    "SetAutomationKeyframeInterpolation undo: 丢弃损坏的 "
                    "automation 轨 %r: %s", param, exc,
                )
        new_track = track._rebuild(automation=new_auto)
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetAutomationKeyframeInterpolationCommand",
            "label": self.label, "track_id": self.track_id,
            "param": self.param, "frame": self.frame,
            "interpolation": self.interpolation,
            "old_track_snapshot": self._old_track_snapshot,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetAutomationKeyframeInterpolationCommand":
        return SetAutomationKeyframeInterpolationCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Automation Interpolation"),
            track_id=data.get("track_id", ""), param=data.get("param", "volume"),
            frame=int(data.get("frame", 0)),
            interpolation=data.get("interpolation", "linear"),
            _old_track_snapshot=data.get("old_track_snapshot"),
        )

    @staticmethod
    def create(track_id: str, param: str, frame: int,
               interpolation: str) -> "SetAutomationKeyframeInterpolationCommand":
        return SetAutomationKeyframeInterpolationCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set {param} Automation Interpolation @ F{frame}",
            track_id=track_id, param=param, frame=frame,
            interpolation=interpolation,
        )


# ── W26/W27: Bus 路由命令 ──

@dataclass(frozen=True)
class AddBusCommand:
    """Add a new Bus track (W27)."""
    command_id: str
    label: str
    bus_name: str = "Bus"
    _new_bus_id: Optional[str] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "AddBusCommand"]:
        from src.features.edit.core.timeline_v2 import EditTrack
        bus_id = str(uuid.uuid4())
        index = len(timeline.audio_tracks)
        bus_track = EditTrack(
            track_id=bus_id, name=self.bus_name, track_type="audio",
            index=index, is_bus=True,
        )
        new_timeline = timeline.with_track_added(bus_track)
        updated_cmd = AddBusCommand(
            command_id=self.command_id, label=self.label,
            bus_name=self.bus_name, _new_bus_id=bus_id,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._new_bus_id is None:
            return timeline
        return timeline.with_track_removed(self._new_bus_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id, "command_type": "AddBusCommand",
            "label": self.label, "bus_name": self.bus_name,
            "new_bus_id": self._new_bus_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AddBusCommand":
        return AddBusCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Add Bus"),
            bus_name=data.get("bus_name", "Bus"),
            _new_bus_id=data.get("new_bus_id"),
        )

    @staticmethod
    def create(bus_name: str = "Bus") -> "AddBusCommand":
        return AddBusCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add Bus: {bus_name}", bus_name=bus_name,
        )


@dataclass(frozen=True)
class RemoveBusCommand:
    """Remove a Bus track and reroute dependent tracks to Master (W27)."""
    command_id: str
    label: str
    bus_id: str
    _old_bus: Optional[Any] = None  # 被删 Bus 的快照（EditTrack）
    # AUD-16206: 完整快照每条被改轨道的 (track_id, old_output_bus_id,
    # old_sends)。旧格式（历史序列化数据）只有 (track_id, old_output_bus_id)
    # 二元组，undo 按长度兼容读取。
    _rerouted_tracks: Optional[list] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "RemoveBusCommand"]:
        bus = timeline.get_track_by_id(self.bus_id)
        if bus is None:
            raise ValueError(f"Bus not found: {self.bus_id}")
        old_bus = bus
        # 找所有路由到该 Bus 的轨道，重路由到 Master
        rerouted: list = []
        new_timeline = timeline
        for track in new_timeline.audio_tracks:
            if track.track_id == self.bus_id:
                continue
            reroutes_output = track.output_bus_id == self.bus_id
            removed_sends = tuple(s for s in track.sends if s.target_bus_id == self.bus_id)
            if reroutes_output or removed_sends:
                # AUD-16206: 删除 Bus 会同时清掉指向它的 sends，undo 必须
                # 一并恢复（含 level/enabled），否则 sends 永久丢失。
                rerouted.append((track.track_id, track.output_bus_id, track.sends))
            if reroutes_output:
                new_track = track.with_output_bus(None)
                new_timeline = new_timeline.with_track_updated(new_track)
            # 移除指向该 Bus 的 sends
            if removed_sends:
                new_sends = tuple(s for s in track.sends if s.target_bus_id != self.bus_id)
                updated = new_timeline.get_track_by_id(track.track_id)
                if updated is not None and updated.sends != new_sends:
                    new_timeline = new_timeline.with_track_updated(updated.with_sends(new_sends))
        # 删除 Bus
        new_timeline = new_timeline.with_track_removed(self.bus_id)
        updated_cmd = RemoveBusCommand(
            command_id=self.command_id, label=self.label, bus_id=self.bus_id,
            _old_bus=old_bus, _rerouted_tracks=rerouted,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_bus is None:
            return timeline
        # 恢复 Bus
        new_timeline = timeline.with_track_added(self._old_bus)
        # 恢复路由与 sends
        for entry in (self._rerouted_tracks or []):
            if len(entry) >= 3:
                track_id, old_output_bus, old_sends = entry[0], entry[1], entry[2]
            else:
                # 旧格式快照（仅 output_bus）：sends 未被记录，保持现状。
                track_id, old_output_bus = entry
                old_sends = None
            track = new_timeline.get_track_by_id(track_id)
            if track is not None:
                if old_sends is not None:
                    track = track.with_sends(tuple(old_sends))
                new_timeline = new_timeline.with_track_updated(track.with_output_bus(old_output_bus))
        return new_timeline

    def to_dict(self) -> dict[str, Any]:

        serialized_reroutes = []
        for entry in (self._rerouted_tracks or []):
            if len(entry) >= 3:
                track_id, old_output_bus, old_sends = entry[0], entry[1], entry[2]
                serialized_reroutes.append([
                    track_id, old_output_bus,
                    [s.to_dict() if hasattr(s, "to_dict") else s for s in (old_sends or ())],
                ])
            else:
                serialized_reroutes.append([entry[0], entry[1]])
        return {
            "command_id": self.command_id, "command_type": "RemoveBusCommand",
            "label": self.label, "bus_id": self.bus_id,
            "old_bus": self._old_bus.to_dict() if self._old_bus is not None else None,
            "rerouted_tracks": serialized_reroutes,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RemoveBusCommand:
        from src.features.edit.core.timeline_v2 import EditTrack, Send

        old_bus = None
        ob = data.get("old_bus")
        if ob:
            try:
                old_bus = EditTrack.from_dict(ob)
            except Exception:
                logger.warning(
                    "RemoveBusCommand.from_dict: old_bus snapshot failed to "
                    "deserialize; undo will be a no-op for this entry"
                )
        rerouted: list = []
        for entry in (data.get("rerouted_tracks") or []):
            try:
                if len(entry) >= 3:
                    sends_data = entry[2] or []
                    sends = tuple(
                        s if isinstance(s, Send) else Send.from_dict(s)
                        for s in sends_data
                    )
                    rerouted.append((entry[0], entry[1], sends))
                else:
                    rerouted.append((entry[0], entry[1]))
            except (TypeError, ValueError, KeyError) as exc:
                logger.warning(
                    "RemoveBusCommand.from_dict: skipping malformed reroute "
                    "entry %r (%s)", entry, exc
                )
        return RemoveBusCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Remove Bus"),
            bus_id=data.get("bus_id", ""),
            _old_bus=old_bus, _rerouted_tracks=rerouted or None,
        )

    @staticmethod
    def create(bus_id: str) -> "RemoveBusCommand":
        return RemoveBusCommand(
            command_id=str(uuid.uuid4()),
            label="Remove Bus", bus_id=bus_id,
        )


@dataclass(frozen=True)
class SetTrackOutputBusCommand:
    """Set a track's output Bus (W27). None=Master."""
    command_id: str
    label: str
    track_id: str
    output_bus_id: Optional[str]
    _old_output_bus_id: Optional[str] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "SetTrackOutputBusCommand"]:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old = track.output_bus_id
        new_track = track.with_output_bus(self.output_bus_id)
        new_timeline = timeline.with_track_updated(new_track)
        updated_cmd = SetTrackOutputBusCommand(
            command_id=self.command_id, label=self.label, track_id=self.track_id,
            output_bus_id=self.output_bus_id, _old_output_bus_id=old,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        return timeline.with_track_updated(track.with_output_bus(self._old_output_bus_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id, "command_type": "SetTrackOutputBusCommand",
            "label": self.label, "track_id": self.track_id,
            "output_bus_id": self.output_bus_id, "old_output_bus_id": self._old_output_bus_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetTrackOutputBusCommand":
        return SetTrackOutputBusCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Track Output Bus"),
            track_id=data.get("track_id", ""),
            output_bus_id=data.get("output_bus_id"),
            _old_output_bus_id=data.get("old_output_bus_id"),
        )

    @staticmethod
    def create(track_id: str, output_bus_id: Optional[str]) -> "SetTrackOutputBusCommand":
        return SetTrackOutputBusCommand(
            command_id=str(uuid.uuid4()),
            label="Set Track Output Bus",
            track_id=track_id, output_bus_id=output_bus_id,
        )


@dataclass(frozen=True)
class AddSendCommand:
    """Add a send to a track (W27)."""
    command_id: str
    label: str
    track_id: str
    target_bus_id: str
    level: float = 1.0
    _new_send_id: Optional[str] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "AddSendCommand"]:
        from src.features.edit.core.timeline_v2 import Send
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        send = Send(target_bus_id=self.target_bus_id, level=self.level)
        new_track = track.with_send_added(send)
        new_timeline = timeline.with_track_updated(new_track)
        updated_cmd = AddSendCommand(
            command_id=self.command_id, label=self.label, track_id=self.track_id,
            target_bus_id=self.target_bus_id, level=self.level,
            _new_send_id=send.send_id,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._new_send_id is None:
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        return timeline.with_track_updated(track.with_send_removed(self._new_send_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id, "command_type": "AddSendCommand",
            "label": self.label, "track_id": self.track_id,
            "target_bus_id": self.target_bus_id, "level": self.level,
            "new_send_id": self._new_send_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AddSendCommand":
        return AddSendCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Add Send"),
            track_id=data.get("track_id", ""),
            target_bus_id=data.get("target_bus_id", ""),
            level=float(data.get("level", 1.0)),
            _new_send_id=data.get("new_send_id"),
        )

    @staticmethod
    def create(track_id: str, target_bus_id: str, level: float = 1.0) -> "AddSendCommand":
        return AddSendCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add Send → {target_bus_id}",
            track_id=track_id, target_bus_id=target_bus_id, level=level,
        )


@dataclass(frozen=True)
class RemoveSendCommand:
    """Remove a send from a track (W27)."""
    command_id: str
    label: str
    track_id: str
    send_id: str
    _old_send: Optional[Any] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "RemoveSendCommand"]:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old_send = None
        for s in track.sends:
            if s.send_id == self.send_id:
                old_send = s
                break
        new_track = track.with_send_removed(self.send_id)
        new_timeline = timeline.with_track_updated(new_track)
        updated_cmd = RemoveSendCommand(
            command_id=self.command_id, label=self.label, track_id=self.track_id,
            send_id=self.send_id, _old_send=old_send,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_send is None:
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        return timeline.with_track_updated(track.with_send_added(self._old_send))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id, "command_type": "RemoveSendCommand",
            "label": self.label, "track_id": self.track_id, "send_id": self.send_id,
            "old_send": self._old_send.to_dict() if self._old_send is not None else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RemoveSendCommand":
        from src.features.edit.core.timeline_v2 import Send
        old_send = None
        osd = data.get("old_send")
        if osd:
            try:
                old_send = Send.from_dict(osd)
            except Exception as e:
                logger.warning(
                    f"RemoveSendCommand.from_dict: old send parse failed: {e}")
        return RemoveSendCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Remove Send"),
            track_id=data.get("track_id", ""), send_id=data.get("send_id", ""),
            _old_send=old_send,
        )

    @staticmethod
    def create(track_id: str, send_id: str) -> "RemoveSendCommand":
        return RemoveSendCommand(
            command_id=str(uuid.uuid4()),
            label="Remove Send", track_id=track_id, send_id=send_id,
        )


@dataclass(frozen=True)
class UpdateSendCommand:
    """Update a send's level/enabled (AM-08 阶段 4.2 Routing UI).

    此前缺此命令：send level/enabled 变更无独立 undo entry。
    现由 Routing UI 的 send-level slider / enable toggle 提交。
    """
    command_id: str
    label: str
    track_id: str
    send_id: str
    new_level: Optional[float] = None      # None=不改 level
    new_enabled: Optional[bool] = None     # None=不改 enabled
    new_target_bus_id: Optional[str] = None  # None=不改目标
    _old_level: Optional[float] = None
    _old_enabled: Optional[bool] = None
    _old_target_bus_id: Optional[str] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "UpdateSendCommand"]:
        from src.features.edit.core.timeline_v2 import Send
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old_send = None
        for s in track.sends:
            if s.send_id == self.send_id:
                old_send = s
                break
        if old_send is None:
            raise ValueError(f"Send not found: {self.send_id}")

        # 构造新 Send（不可变，逐字段替换）
        new_level = self.new_level if self.new_level is not None else old_send.level
        new_enabled = self.new_enabled if self.new_enabled is not None else old_send.enabled
        new_target = self.new_target_bus_id if self.new_target_bus_id is not None else old_send.target_bus_id
        new_send = Send(
            send_id=old_send.send_id, target_bus_id=new_target,
            level=float(new_level), enabled=bool(new_enabled),
        )
        new_track = track.with_send_updated(new_send)
        new_timeline = timeline.with_track_updated(new_track)
        updated_cmd = UpdateSendCommand(
            command_id=self.command_id, label=self.label, track_id=self.track_id,
            send_id=self.send_id,
            new_level=self.new_level, new_enabled=self.new_enabled,
            new_target_bus_id=self.new_target_bus_id,
            _old_level=old_send.level, _old_enabled=old_send.enabled,
            _old_target_bus_id=old_send.target_bus_id,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        from src.features.edit.core.timeline_v2 import Send
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        # 还原到旧值
        old_send = None
        for s in track.sends:
            if s.send_id == self.send_id:
                old_send = s
                break
        if old_send is None:
            return timeline
        restored = Send(
            send_id=old_send.send_id, target_bus_id=self._old_target_bus_id or old_send.target_bus_id,
            level=self._old_level if self._old_level is not None else old_send.level,
            enabled=self._old_enabled if self._old_enabled is not None else old_send.enabled,
        )
        return timeline.with_track_updated(track.with_send_updated(restored))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id, "command_type": "UpdateSendCommand",
            "label": self.label, "track_id": self.track_id, "send_id": self.send_id,
            "new_level": self.new_level, "new_enabled": self.new_enabled,
            "new_target_bus_id": self.new_target_bus_id,
            "old_level": self._old_level, "old_enabled": self._old_enabled,
            "old_target_bus_id": self._old_target_bus_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "UpdateSendCommand":
        return UpdateSendCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Update Send"),
            track_id=data.get("track_id", ""),
            send_id=data.get("send_id", ""),
            new_level=data.get("new_level"),
            new_enabled=data.get("new_enabled"),
            new_target_bus_id=data.get("new_target_bus_id"),
            _old_level=data.get("old_level"),
            _old_enabled=data.get("old_enabled"),
            _old_target_bus_id=data.get("old_target_bus_id"),
        )

    @staticmethod
    def create(track_id: str, send_id: str, *, level: Optional[float] = None,
               enabled: Optional[bool] = None, target_bus_id: Optional[str] = None) -> "UpdateSendCommand":
        parts = []
        if level is not None:
            parts.append(f"level={level}")
        if enabled is not None:
            parts.append(f"enabled={enabled}")
        if target_bus_id is not None:
            parts.append(f"target={target_bus_id}")
        return UpdateSendCommand(
            command_id=str(uuid.uuid4()),
            label=f"Update Send ({', '.join(parts) or 'no-op'})",
            track_id=track_id, send_id=send_id,
            new_level=level, new_enabled=enabled, new_target_bus_id=target_bus_id,
        )
