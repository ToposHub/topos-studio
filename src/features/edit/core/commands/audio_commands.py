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
from src.shared.track_colors import next_track_color, resolve_track_color

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
class SetTrackAutomationModeCommand:
    """§7 P1：轨道自动化模式（off/read/write/touch/latch）入时间线历史。

    引擎按轨道模式读取曲线/写入关键帧（AM-07 签名含 automation_mode）；
    模式切换此前只改控制器全局态，模型与引擎不随动——Read 打开后曲线
    实际不回放。命令化后切换可撤销、重开可还原。
    """

    command_id: str
    label: str
    track_id: str
    mode: str
    _old_mode: str | None = None

    _VALID = ("off", "read", "write", "touch", "latch")

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        normalized = str(self.mode or "off").lower()
        if normalized not in self._VALID:
            raise ValueError(f"Unsupported automation mode: {self.mode!r}")
        old_mode = track.automation_mode
        new_timeline = timeline.with_track_updated(
            track.with_automation_mode(normalized))
        updated_cmd = SetTrackAutomationModeCommand(
            command_id=self.command_id, label=self.label,
            track_id=self.track_id, mode=self.mode, _old_mode=old_mode)
        logger.debug(
            "SetTrackAutomationModeCommand applied: track %s mode=%s",
            self.track_id, normalized)
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_mode is None:
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        return timeline.with_track_updated(
            track.with_automation_mode(self._old_mode))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackAutomationModeCommand",
            "label": self.label,
            "track_id": self.track_id,
            "mode": self.mode,
            "old_mode": self._old_mode,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetTrackAutomationModeCommand:
        return SetTrackAutomationModeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Automation Mode"),
            track_id=data.get("track_id", ""),
            mode=data.get("mode", "off"),
            _old_mode=data.get("old_mode"),
        )

    @staticmethod
    def create(track_id: str, mode: str) -> SetTrackAutomationModeCommand:
        normalized = str(mode or "off").lower()
        display = normalized.capitalize()
        return SetTrackAutomationModeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Automation Mode: {display}",
            track_id=track_id, mode=normalized)


@dataclass(frozen=True)
class SetTrackAutomationTrimCommand:
    """P1-5：Trim Automation——某参数的恒定倍率（相对既有曲线偏移）。

    volume 为线性域：0.5 ≈ −6 dB、2.0 ≈ +6 dB；None=清除 trim。
    求值点：EditTrack.get_automation_value 与引擎逐样本 ramp（同语义）。
    """

    command_id: str
    label: str
    track_id: str
    param: str
    factor: float | None
    _old_factor: float | None = None

    _FACTOR_MIN, _FACTOR_MAX = 0.01, 4.0

    def apply(self, timeline: TimelineSequence):
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old = track.automation_trim_factor(self.param)
        old_effective = old if old != 1.0 else None
        new_timeline = timeline.with_track_updated(
            track.with_automation_trim(self.param, self.factor))
        updated_cmd = SetTrackAutomationTrimCommand(
            command_id=self.command_id, label=self.label,
            track_id=self.track_id, param=self.param, factor=self.factor,
            _old_factor=old_effective)
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        return timeline.with_track_updated(
            track.with_automation_trim(self.param, self._old_factor))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackAutomationTrimCommand",
            "label": self.label,
            "track_id": self.track_id,
            "param": self.param,
            "factor": self.factor,
            "old_factor": self._old_factor,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetTrackAutomationTrimCommand":
        factor = data.get("factor")
        return SetTrackAutomationTrimCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Automation Trim"),
            track_id=data.get("track_id", ""),
            param=data.get("param", "volume"),
            factor=(float(factor) if factor is not None else None),
            _old_factor=(float(data["old_factor"])
                         if data.get("old_factor") is not None else None),
        )

    @staticmethod
    def create(track_id: str, param: str,
               factor: float | None) -> "SetTrackAutomationTrimCommand":
        return SetTrackAutomationTrimCommand(
            command_id=str(uuid.uuid4()),
            label=(f"Trim automation {param} ×{factor:g}"
                   if factor is not None else
                   f"Clear automation trim {param}"),
            track_id=track_id, param=param, factor=factor)


@dataclass(frozen=True)
class SetTrackAutomationWriteProtectCommand:
    """P1-5：自动化写保护——保护轨的录制入口拒绝采集与写模式切换。"""

    command_id: str
    label: str
    track_id: str
    protected: bool
    _old_protected: bool | None = None

    def apply(self, timeline: TimelineSequence):
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old = bool(track.automation_write_protected)
        new_timeline = timeline.with_track_updated(
            track.with_automation_write_protected(bool(self.protected)))
        updated_cmd = SetTrackAutomationWriteProtectCommand(
            command_id=self.command_id, label=self.label,
            track_id=self.track_id, protected=bool(self.protected),
            _old_protected=old)
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_protected is None:
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        return timeline.with_track_updated(
            track.with_automation_write_protected(bool(self._old_protected)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackAutomationWriteProtectCommand",
            "label": self.label,
            "track_id": self.track_id,
            "protected": self.protected,
            "old_protected": self._old_protected,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetTrackAutomationWriteProtectCommand":
        return SetTrackAutomationWriteProtectCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Automation Write Protect"),
            track_id=data.get("track_id", ""),
            protected=bool(data.get("protected", False)),
            _old_protected=(bool(data["old_protected"])
                            if data.get("old_protected") is not None else None),
        )

    @staticmethod
    def create(track_id: str, protected: bool) -> "SetTrackAutomationWriteProtectCommand":
        return SetTrackAutomationWriteProtectCommand(
            command_id=str(uuid.uuid4()),
            label=("Protect automation on track" if protected
                   else "Unprotect automation on track"),
            track_id=track_id, protected=bool(protected))


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
class SetTrackPannerCommand:
    """
    Command to set track spatial panner params (CW-A1).

    Panner is stored as a dict (SpatialPannerParams.to_dict()); empty dict
    means "not in use" (fallback to plain pan/balance). The lr value itself
    lives in EditTrack.pan (see SetTrackPanCommand) — this command only
    covers the remaining panner fields.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID to set panner
        panner: New panner dict ({} = disable)
        _old_panner: Old panner dict (for undo)
    """

    command_id: str
    label: str
    track_id: str
    panner: dict
    _old_panner: dict | None = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "SetTrackPannerCommand"]:
        """Apply the command: set panner."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        old_panner = dict(track.panner or {})
        new_track = track.with_panner(self.panner)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(
            "SetTrackPannerCommand applied: track %s panner keys=%d",
            self.track_id, len(self.panner or {}),
        )
        updated_cmd = SetTrackPannerCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            panner=self.panner,
            _old_panner=old_panner,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old panner."""
        if self._old_panner is None:
            logger.warning("SetTrackPannerCommand undo: old_panner not stored")
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        new_timeline = timeline.with_track_updated(track.with_panner(self._old_panner))
        logger.debug("SetTrackPannerCommand undone: track %s", self.track_id)
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackPannerCommand",
            "label": self.label,
            "track_id": self.track_id,
            "panner": dict(self.panner or {}),
            "old_panner": dict(self._old_panner or {}) if self._old_panner else {},
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetTrackPannerCommand:
        return SetTrackPannerCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Spatial Panner"),
            track_id=data.get("track_id", ""),
            panner=dict(data.get("panner") or {}),
            _old_panner=dict(data.get("old_panner") or {}) if data.get("old_panner") else None,
        )

    @staticmethod
    def create(track_id: str, panner: dict) -> SetTrackPannerCommand:
        return SetTrackPannerCommand(
            command_id=str(uuid.uuid4()),
            label="Set Spatial Panner",
            track_id=track_id,
            panner=dict(panner or {}),
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

        from src.features.edit.core.native_mix_layout import validate_rack_edit

        validate_rack_edit(timeline, self.track_id, self.effect_chain)
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
class UpgradeTrackEffectAlgorithmCommand:
    """H01：显式把轨道 FX 链的 algorithm_version 升级到目标版本。

    语义（fx_versioning.upgrade_chain_algorithm_version）：
    - 只改 ``algorithm_version``，不动声音参数——升级后实例按新版本算法
      渲染（声音可能改变，这正是"显式升级"的含义）；
    - future（> 本地版本）实例原样保留，不伪装成本地版本；
    - 经 CommandStack 执行 → 天然 undo / 保存重开可核对。

    Attributes:
        target_version: 目标算法版本（None = 本地当前版本）
    """

    command_id: str
    label: str
    track_id: str
    target_version: Optional[int] = None
    _old_effect_chain: Optional[Any] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "UpgradeTrackEffectAlgorithmCommand"]:
        from src.features.edit.core.fx_versioning import (
            upgrade_chain_algorithm_version,
        )
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old_chain = track.effect_chain
        if old_chain is None or not old_chain.effects:
            # 空链：无操作（保留原时间线对象）
            return timeline, UpgradeTrackEffectAlgorithmCommand(
                command_id=self.command_id, label=self.label,
                track_id=self.track_id,
                target_version=self.target_version,
                _old_effect_chain=old_chain)
        upgraded = upgrade_chain_algorithm_version(
            old_chain, target=self.target_version)
        new_track = track.with_effect_chain(upgraded)
        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(
            "UpgradeTrackEffectAlgorithmCommand applied: track %s -> v%s",
            self.track_id,
            self.target_version or "current")
        return new_timeline, UpgradeTrackEffectAlgorithmCommand(
            command_id=self.command_id, label=self.label,
            track_id=self.track_id,
            target_version=self.target_version,
            _old_effect_chain=old_chain)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        new_track = track.with_effect_chain(self._old_effect_chain)
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "UpgradeTrackEffectAlgorithmCommand",
            "label": self.label,
            "track_id": self.track_id,
            "target_version": self.target_version,
            "old_effect_chain": self._old_effect_chain.to_dict()
            if self._old_effect_chain is not None else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "UpgradeTrackEffectAlgorithmCommand":
        from src.features.edit.core.timeline_v2 import EffectChainParams
        old = data.get("old_effect_chain")
        return UpgradeTrackEffectAlgorithmCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Upgrade FX Algorithm"),
            track_id=data.get("track_id", ""),
            target_version=data.get("target_version"),
            _old_effect_chain=EffectChainParams.from_dict(old) if old else None,
        )

    @staticmethod
    def create(track_id: str,
               target_version: Optional[int] = None) -> "UpgradeTrackEffectAlgorithmCommand":
        return UpgradeTrackEffectAlgorithmCommand(
            command_id=str(uuid.uuid4()),
            label="Upgrade FX Algorithm",
            track_id=track_id,
            target_version=target_version,
        )


@dataclass(frozen=True)
class SetMasterStripEQCommand:
    """Command to set the Master bus Strip EQ (MX-03).

    Master EQ 与普通轨同一 StripEQParams 模型，在 master 音量后、限幅器前
    应用（AudioMixer.mix_samples 阶段 3 / AudioGraphRenderer.apply_master）。
    None 表示禁用。undo/redo/序列化与轨级命令同构。
    """

    command_id: str
    label: str
    strip_eq: Optional[Any]  # StripEQParams | None
    _old_strip_eq: Optional[Any] = None  # StripEQParams | None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "SetMasterStripEQCommand"]:
        old_strip_eq = timeline.master_strip_eq
        new_timeline = timeline.with_master_strip_eq(self.strip_eq)
        logger.debug("SetMasterStripEQCommand applied")
        updated_cmd = SetMasterStripEQCommand(
            command_id=self.command_id,
            label=self.label,
            strip_eq=self.strip_eq,
            _old_strip_eq=old_strip_eq,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        return timeline.with_master_strip_eq(self._old_strip_eq)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetMasterStripEQCommand",
            "label": self.label,
            "strip_eq": self.strip_eq.to_dict() if self.strip_eq is not None else None,
            "old_strip_eq": self._old_strip_eq.to_dict() if self._old_strip_eq is not None else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetMasterStripEQCommand":
        from src.features.edit.core.timeline_v2 import StripEQParams
        se = data.get("strip_eq")
        old = data.get("old_strip_eq")
        return SetMasterStripEQCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Master Strip EQ"),
            strip_eq=StripEQParams.from_dict(se) if se else None,
            _old_strip_eq=StripEQParams.from_dict(old) if old else None,
        )

    @staticmethod
    def create(strip_eq: Optional[Any]) -> "SetMasterStripEQCommand":
        return SetMasterStripEQCommand(
            command_id=str(uuid.uuid4()),
            label="Set Master Strip EQ",
            strip_eq=strip_eq,
        )


@dataclass(frozen=True)
class SetMasterEffectChainCommand:
    """Command to set the Master bus FX rack chain (MX-03).

    Master FX 与普通轨同一 EffectChainParams 模型（instance_id 身份独立），
    在 master EQ 后、限幅器前应用。None 表示清空链。
    """

    command_id: str
    label: str
    effect_chain: Optional[Any]  # EffectChainParams | None
    _old_effect_chain: Optional[Any] = None  # EffectChainParams | None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "SetMasterEffectChainCommand"]:
        from src.features.edit.core.native_mix_layout import validate_rack_edit

        validate_rack_edit(timeline, None, self.effect_chain)
        old_chain = timeline.master_effect_chain
        new_timeline = timeline.with_master_effect_chain(self.effect_chain)
        logger.debug("SetMasterEffectChainCommand applied")
        updated_cmd = SetMasterEffectChainCommand(
            command_id=self.command_id,
            label=self.label,
            effect_chain=self.effect_chain,
            _old_effect_chain=old_chain,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        return timeline.with_master_effect_chain(self._old_effect_chain)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetMasterEffectChainCommand",
            "label": self.label,
            "effect_chain": self.effect_chain.to_dict() if self.effect_chain is not None else None,
            "old_effect_chain": self._old_effect_chain.to_dict() if self._old_effect_chain is not None else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetMasterEffectChainCommand":
        from src.features.edit.core.timeline_v2 import EffectChainParams
        ec = data.get("effect_chain")
        old = data.get("old_effect_chain")
        return SetMasterEffectChainCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Master FX Chain"),
            effect_chain=EffectChainParams.from_dict(ec) if ec else None,
            _old_effect_chain=EffectChainParams.from_dict(old) if old else None,
        )

    @staticmethod
    def create(effect_chain: Optional[Any]) -> "SetMasterEffectChainCommand":
        return SetMasterEffectChainCommand(
            command_id=str(uuid.uuid4()),
            label="Set Master FX Chain",
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


# ─────────────────────────────────────────────────────────────────────────────
# MX-11: 单一 ChannelStripState capture/apply（预设与快照覆盖同一字段集）
# ─────────────────────────────────────────────────────────────────────────────

def _capture_strip_state(track: Any) -> dict:
    """捕获单条 strip 的完整混音状态（volume/pan/mute/solo/EQ/FX/Panner/
    routing）。预设、快照与 undo 共用同一字段集——不再出现"预设文件存了
    效果但应用没恢复"。"""
    return {
        "volume": float(getattr(track, "volume", 1.0)),
        "pan": float(getattr(track, "pan", 0.0)),
        "is_muted": bool(getattr(track, "is_muted", False)),
        "is_solo": bool(getattr(track, "is_solo", False)),
        "strip_eq": (track.strip_eq.to_dict()
                     if getattr(track, "strip_eq", None) is not None else None),
        "effect_chain": (track.effect_chain.to_dict()
                         if getattr(track, "effect_chain", None) is not None
                         else None),
        # 全量快照语义：空 dict = 目标轨当前无 panner（undo 据此精确恢复）；
        # 与预设文件的 panner=None（未捕获→保留现状）显式区分。
        "panner": dict(getattr(track, "panner", {}) or {}),
        "output_bus_id": getattr(track, "output_bus_id", None),
        "sends": [s.to_dict() if hasattr(s, "to_dict") else dict(s)
                  for s in (getattr(track, "sends", ()) or ())],
    }


def _apply_strip_state(track: Any, ts: dict, full_snapshot: bool = False) -> Any:
    """把 strip 状态 dict 应用到 track。

    两种语义（MX-11 审查修正）：
    - full_snapshot=True（快照 apply/undo、预设 undo 的 _old_state）：
      capture 总是写全字段（panner 捕获为 dict 可空、sends 捕获实际
      元组）——空值就是被捕获的真实状态，逐字段精确恢复（A→B→A）。
    - full_snapshot=False（通道预设 apply，默认）：键缺失或值为 None/
      空表示"预设未捕获该字段"（旧版预设文件只有五字段）——保留目标
      轨现状，不得清掉活动 Panner/路由/Sends。
    """
    from src.features.edit.core.timeline_v2 import (
        EffectChainParams, Send, StripEQParams,
    )
    new_track = track
    if "volume" in ts:
        new_track = new_track.with_volume(float(ts.get("volume", 1.0)))
    if "pan" in ts:
        new_track = new_track.with_pan(float(ts.get("pan", 0.0)))
    if "is_muted" in ts:
        new_track = new_track.with_muted(bool(ts.get("is_muted", False)))
    if "is_solo" in ts:
        new_track = new_track.with_solo(bool(ts.get("is_solo", False)))
    if "strip_eq" in ts:
        se = ts.get("strip_eq")
        try:
            new_track = new_track.with_strip_eq(
                StripEQParams.from_dict(se) if se else None)
        except Exception as e:  # noqa: BLE001 — 坏字段降级不阻断
            logger.warning(f"strip_eq parse failed: {e}")
    if "effect_chain" in ts:
        ec = ts.get("effect_chain")
        try:
            new_track = new_track.with_effect_chain(
                EffectChainParams.from_dict(ec) if ec else None)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"effect_chain parse failed: {e}")
    if "panner" in ts and (ts.get("panner") is not None or full_snapshot):
        try:
            new_track = new_track.with_panner(dict(ts.get("panner") or {}))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"panner parse failed: {e}")
    elif "panner" in ts and ts.get("panner") is None and not full_snapshot:
        # R05：显式捕获为空的 panner（键存在且值为 None）→ 清除
        new_track = new_track.with_panner({})
    if "output_bus_id" in ts and (ts.get("output_bus_id") is not None
                                  or full_snapshot):
        try:
            new_track = new_track.with_output_bus(ts.get("output_bus_id"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"output_bus parse failed: {e}")
    elif "output_bus_id" in ts and ts.get("output_bus_id") is None \
            and not full_snapshot:
        # R05：显式空路由 → 回 Master
        new_track = new_track.with_output_bus(None)
    if "sends" in ts and (ts.get("sends") or full_snapshot):
        try:
            sends = tuple(
                Send.from_dict(sd) if isinstance(sd, dict) else sd
                for sd in (ts.get("sends") or ()))
            new_track = new_track.with_sends(sends)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"sends parse failed: {e}")
    elif "sends" in ts and not ts.get("sends") and not full_snapshot:
        # R05：显式空 sends 列表 → 清空
        new_track = new_track.with_sends(())
    return new_track


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
    # MX-11: 完整 strip 状态（FX 链 / Panner / routing）——预设应用与 undo
    # 覆盖同一字段集
    effect_chain: Optional[Any] = None
    panner: Optional[dict] = None
    output_bus_id: Optional[str] = None
    sends: tuple = ()
    # 旧值（apply 时填充）
    _old_volume: float | None = None
    _old_pan: float | None = None
    _old_muted: bool | None = None
    _old_solo: bool | None = None
    _old_strip_eq: Optional[Any] = None
    _old_state: Optional[dict] = None
    # R05：字段存在性（来自 ChannelStripPreset.explicit_fields）——显式
    # 字段即使为空也写入 new_state（清除语义）；未显式字段保留目标轨现状
    _explicit_fields: frozenset = frozenset()

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "ApplyChannelStripPresetCommand"]:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # MX-11: 记录完整旧状态（undo 逐字段恢复）
        old_state = _capture_strip_state(track)

        # MX-11 审查修正：预设未携带的字段（None/空）从 new_state 省略——
        # 旧版预设文件只有五字段，应用时不得清掉目标轨的活动
        # Panner/路由/Sends（"预设没存的状态保留现状"）。
        new_state = {
            "volume": self.volume, "pan": self.pan,
            "is_muted": self.is_muted, "is_solo": self.is_solo,
        }
        # R05：显式字段（全量捕获的预设）即使为空也进 new_state——
        # _apply_strip_state 按"显式空 = 清除"处理；旧版预设（未显式）
        # 保持"没存就保留现状"的兼容语义。
        _explicit = self._explicit_fields
        if self.strip_eq is not None:
            new_state["strip_eq"] = self.strip_eq.to_dict()
        elif "strip_eq" in _explicit:
            new_state["strip_eq"] = None
        if self.effect_chain is not None:
            new_state["effect_chain"] = self.effect_chain.to_dict()
        elif "effect_chain" in _explicit:
            new_state["effect_chain"] = None
        if self.panner:
            new_state["panner"] = dict(self.panner)
        elif "panner" in _explicit:
            new_state["panner"] = None
        if self.output_bus_id:
            new_state["output_bus_id"] = self.output_bus_id
        elif "output_bus_id" in _explicit:
            new_state["output_bus_id"] = None
        if self.sends:
            new_state["sends"] = [
                s.to_dict() if hasattr(s, "to_dict") else dict(s)
                for s in self.sends]
        elif "sends" in _explicit:
            new_state["sends"] = []
        new_track = _apply_strip_state(track, new_state)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"ApplyChannelStripPresetCommand applied: track {self.track_id}")

        updated_cmd = ApplyChannelStripPresetCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            volume=self.volume, pan=self.pan,
            is_muted=self.is_muted, is_solo=self.is_solo, strip_eq=self.strip_eq,
            effect_chain=self.effect_chain, panner=self.panner,
            output_bus_id=self.output_bus_id, sends=self.sends,
            _old_state=old_state,
            _explicit_fields=self._explicit_fields,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        # MX-11: 旧状态存在时逐字段恢复（含 EQ/FX/Panner/routing）；
        # 旧命令序列化（无 _old_state）回退到仅五字段恢复。
        if self._old_state is not None:
            # _old_state 是全量 capture（含空 dict/空 tuple 的真实状态）
            new_track = _apply_strip_state(track, self._old_state,
                                           full_snapshot=True)
            return timeline.with_track_updated(new_track)
        new_track = track.with_volume(self._old_volume).with_pan(self._old_pan)
        new_track = new_track.with_muted(self._old_muted).with_solo(self._old_solo)
        new_track = new_track.with_strip_eq(self._old_strip_eq)
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "command_id": self.command_id,
            "command_type": "ApplyChannelStripPresetCommand",
            "label": self.label,
            "track_id": self.track_id,
            "volume": self.volume, "pan": self.pan,
            "is_muted": self.is_muted, "is_solo": self.is_solo,
            "strip_eq": self.strip_eq.to_dict() if self.strip_eq is not None else None,
            "effect_chain": (self.effect_chain.to_dict()
                             if self.effect_chain is not None else None),
            "panner": self.panner,
            "output_bus_id": self.output_bus_id,
            "sends": [s.to_dict() if hasattr(s, "to_dict") else dict(s)
                      for s in self.sends],
        }
        if self._old_state is not None:
            d["old_state"] = self._old_state
        else:
            d.update({
                "old_volume": self._old_volume, "old_pan": self._old_pan,
                "old_muted": self._old_muted, "old_solo": self._old_solo,
                "old_strip_eq": (self._old_strip_eq.to_dict()
                                 if self._old_strip_eq is not None else None),
            })
        if self._explicit_fields:
            d["explicit_fields"] = sorted(self._explicit_fields)
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ApplyChannelStripPresetCommand":
        from src.features.edit.core.timeline_v2 import (
            EffectChainParams, Send, StripEQParams,
        )
        se = data.get("strip_eq")
        ec = data.get("effect_chain")
        sends = tuple(
            Send.from_dict(sd) if isinstance(sd, dict) else sd
            for sd in (data.get("sends") or ()))
        old_state = data.get("old_state")
        old_se = data.get("old_strip_eq")
        return ApplyChannelStripPresetCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Apply Strip Preset"),
            track_id=data.get("track_id", ""),
            volume=float(data.get("volume", 1.0)), pan=float(data.get("pan", 0.0)),
            is_muted=bool(data.get("is_muted", False)), is_solo=bool(data.get("is_solo", False)),
            strip_eq=StripEQParams.from_dict(se) if se else None,
            effect_chain=EffectChainParams.from_dict(ec) if ec else None,
            panner=data.get("panner"),
            output_bus_id=data.get("output_bus_id"),
            sends=sends,
            _old_state=old_state,
            _old_volume=data.get("old_volume"), _old_pan=data.get("old_pan"),
            _old_muted=data.get("old_muted"), _old_solo=data.get("old_solo"),
            _old_strip_eq=StripEQParams.from_dict(old_se) if old_se else None,
            _explicit_fields=frozenset(data.get("explicit_fields") or ()),
        )

    @staticmethod
    def create_from_preset(track_id: str, preset: Any) -> "ApplyChannelStripPresetCommand":
        """从 ChannelStripPreset 构造命令（MX-11: 全字段）。"""
        return ApplyChannelStripPresetCommand(
            command_id=str(uuid.uuid4()),
            label=f"Apply Preset: {preset.name}",
            track_id=track_id,
            volume=preset.volume, pan=preset.pan,
            is_muted=preset.is_muted, is_solo=preset.is_solo,
            strip_eq=preset.strip_eq,
            effect_chain=getattr(preset, "effect_chain", None),
            panner=getattr(preset, "panner", None),
            output_bus_id=getattr(preset, "output_bus_id", None),
            sends=tuple(getattr(preset, "sends", ()) or ()),
            _explicit_fields=frozenset(
                getattr(preset, "explicit_fields", frozenset()) or ()),
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
    # R05：Master 条带全字段。"_unset"=旧快照未捕获（应用时保留现状）
    master_strip_eq: Any = "_unset"
    master_effect_chain: Any = "_unset"
    _old_snapshot: Optional[dict] = None

    def _capture_current(self, timeline: TimelineSequence) -> dict:
        """捕获当前 timeline 的混音状态为 dict（用于 undo；MX-11/R05 全字段）。"""
        track_states: dict[str, dict] = {}
        for track in timeline.audio_tracks:
            track_states[track.track_id] = _capture_strip_state(track)
        return {
            "track_states": track_states,
            "master_volume": timeline.master_volume,
            "master_muted": timeline.master_muted,
            # R05：Master EQ/FX 一并捕获（undo 恢复同一字段集；dict 序列化
            # 与命令 to_dict/旧快照格式一致）
            "master_strip_eq": (timeline.master_strip_eq.to_dict()
                                if timeline.master_strip_eq is not None else None),
            "master_effect_chain": (timeline.master_effect_chain.to_dict()
                                    if timeline.master_effect_chain is not None
                                    else None),
        }

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "ApplyMixSnapshotCommand"]:
        old_snapshot = self._capture_current(timeline)
        new_timeline = timeline
        # 应用每条轨道（MX-11: 全字段走统一 helper）
        for track in new_timeline.audio_tracks:
            ts = self.track_states.get(track.track_id)
            if ts is None:
                continue
            # MixSnapshot.track_states 来自全量 capture（ChannelStripPreset
            # .from_track）：空值 = 被捕获的真实状态
            new_track = _apply_strip_state(track, ts, full_snapshot=True)
            new_timeline = new_timeline.with_track_updated(new_track)
        # Master（R05：EQ/FX 一并恢复；"_unset" = 旧快照未捕获 → 保留现状）
        new_timeline = new_timeline.with_master_volume(self.master_volume)
        new_timeline = new_timeline.with_master_muted(self.master_muted)
        new_timeline = self._restore_master_strip(new_timeline, self)

        updated_cmd = ApplyMixSnapshotCommand(
            command_id=self.command_id, label=self.label,
            snapshot_name=self.snapshot_name, track_states=self.track_states,
            master_volume=self.master_volume, master_muted=self.master_muted,
            master_strip_eq=self.master_strip_eq,
            master_effect_chain=self.master_effect_chain,
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
            # MX-11: undo 与 apply 同一字段集（旧实现漏恢复 strip_eq/FX）
            new_track = _apply_strip_state(track, ts, full_snapshot=True)
            new_timeline = new_timeline.with_track_updated(new_track)
        new_timeline = new_timeline.with_master_volume(self._old_snapshot.get("master_volume", 1.0))
        new_timeline = new_timeline.with_master_muted(self._old_snapshot.get("master_muted", False))
        # R05：undo 恢复 Master EQ/FX（旧格式 _old_snapshot 无该键 → 保留现状）
        new_timeline = self._restore_master_strip(new_timeline, self._old_snapshot)
        return new_timeline

    def _restore_master_strip(self, timeline: TimelineSequence,
                              source: Any) -> TimelineSequence:
        """R05：从命令参数或旧快照 dict 恢复 Master EQ/FX。

        "_unset"（旧快照未捕获）→ 保留 timeline 现状；其余值（含 None）
        按快照真实状态恢复。
        """
        from src.features.edit.core.timeline_v2 import (
            EffectChainParams, StripEQParams,
        )
        se = getattr(source, "master_strip_eq", None)
        if isinstance(source, dict):
            if "master_strip_eq" not in source:
                se = "_unset"
            else:
                se = source.get("master_strip_eq")
        if se != "_unset":
            try:
                timeline = timeline.with_master_strip_eq(
                    StripEQParams.from_dict(se) if se else None)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"master_strip_eq parse failed: {e}")
        ec = getattr(source, "master_effect_chain", None)
        if isinstance(source, dict):
            if "master_effect_chain" not in source:
                ec = "_unset"
            else:
                ec = source.get("master_effect_chain")
        if ec != "_unset":
            try:
                timeline = timeline.with_master_effect_chain(
                    EffectChainParams.from_dict(ec) if ec else None)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"master_effect_chain parse failed: {e}")
        return timeline

    def to_dict(self) -> dict[str, Any]:
        d = {
            "command_id": self.command_id,
            "command_type": "ApplyMixSnapshotCommand",
            "label": self.label,
            "snapshot_name": self.snapshot_name,
            "track_states": self.track_states,
            "master_volume": self.master_volume,
            "master_muted": self.master_muted,
            "old_snapshot": self._old_snapshot,
        }
        if self.master_strip_eq != "_unset":
            d["master_strip_eq"] = (
                self.master_strip_eq.to_dict()
                if hasattr(self.master_strip_eq, "to_dict")
                else self.master_strip_eq)
        if self.master_effect_chain != "_unset":
            d["master_effect_chain"] = (
                self.master_effect_chain.to_dict()
                if hasattr(self.master_effect_chain, "to_dict")
                else self.master_effect_chain)
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ApplyMixSnapshotCommand":
        return ApplyMixSnapshotCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Apply Mix Snapshot"),
            snapshot_name=data.get("snapshot_name", "Snapshot"),
            track_states=data.get("track_states", {}),
            master_volume=float(data.get("master_volume", 1.0)),
            master_muted=bool(data.get("master_muted", False)),
            master_strip_eq=data.get("master_strip_eq", "_unset"),
            master_effect_chain=data.get("master_effect_chain", "_unset"),
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
            master_strip_eq=getattr(snapshot, "master_strip_eq", "_unset"),
            master_effect_chain=getattr(snapshot, "master_effect_chain",
                                        "_unset"),
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
            color=next_track_color(
                resolve_track_color(track.track_id, track.color)
                for track in timeline.all_tracks
            ),
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
    """Add a send to a track (W27; MX-14 扩展 tap/muted)."""
    command_id: str
    label: str
    track_id: str
    target_bus_id: str
    level: float = 1.0
    tap: str = "post_fader"   # MX-14
    muted: bool = False       # MX-14
    _new_send_id: Optional[str] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "AddSendCommand"]:
        from src.features.edit.core.timeline_v2 import Send
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        send = Send(target_bus_id=self.target_bus_id, level=self.level,
                    tap=self.tap, muted=self.muted)
        new_track = track.with_send_added(send)
        new_timeline = timeline.with_track_updated(new_track)
        updated_cmd = AddSendCommand(
            command_id=self.command_id, label=self.label, track_id=self.track_id,
            target_bus_id=self.target_bus_id, level=self.level,
            tap=self.tap, muted=self.muted,
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
            "tap": self.tap, "muted": self.muted,
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
            tap=str(data.get("tap", "post_fader") or "post_fader"),
            muted=bool(data.get("muted", False)),
            _new_send_id=data.get("new_send_id"),
        )

    @staticmethod
    def create(track_id: str, target_bus_id: str, level: float = 1.0,
               tap: str = "post_fader", muted: bool = False) -> "AddSendCommand":
        return AddSendCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add Send → {target_bus_id}",
            track_id=track_id, target_bus_id=target_bus_id, level=level,
            tap=tap, muted=muted,
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
    """Update a send's level/enabled/target/tap/muted（AM-08 + MX-14）。

    此前缺此命令：send level/enabled 变更无独立 undo entry。
    现由 Routing UI 的 send-level slider / enable toggle / tap 选择 / mute 提交。
    MX-14: apply/undo 重建 Send 时保留未变更字段（含 tap/muted）——旧实现
    重建丢字段，改 level 会把 pre_fader/muted 静默复位。
    """
    command_id: str
    label: str
    track_id: str
    send_id: str
    new_level: Optional[float] = None      # None=不改 level
    new_enabled: Optional[bool] = None     # None=不改 enabled
    new_target_bus_id: Optional[str] = None  # None=不改目标
    new_tap: Optional[str] = None          # MX-14: None=不改 tap 位置
    new_muted: Optional[bool] = None       # MX-14: None=不改独立 mute
    _old_level: Optional[float] = None
    _old_enabled: Optional[bool] = None
    _old_target_bus_id: Optional[str] = None
    _old_tap: Optional[str] = None
    _old_muted: Optional[bool] = None

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

        # 构造新 Send（不可变，逐字段替换；未指定字段保留旧值）
        new_level = self.new_level if self.new_level is not None else old_send.level
        new_enabled = self.new_enabled if self.new_enabled is not None else old_send.enabled
        new_target = self.new_target_bus_id if self.new_target_bus_id is not None else old_send.target_bus_id
        new_tap = self.new_tap if self.new_tap is not None else old_send.tap
        new_muted = self.new_muted if self.new_muted is not None else old_send.muted
        new_send = Send(
            send_id=old_send.send_id, target_bus_id=new_target,
            level=float(new_level), enabled=bool(new_enabled),
            tap=str(new_tap), muted=bool(new_muted),
        )
        new_track = track.with_send_updated(new_send)
        new_timeline = timeline.with_track_updated(new_track)
        updated_cmd = UpdateSendCommand(
            command_id=self.command_id, label=self.label, track_id=self.track_id,
            send_id=self.send_id,
            new_level=self.new_level, new_enabled=self.new_enabled,
            new_target_bus_id=self.new_target_bus_id,
            new_tap=self.new_tap, new_muted=self.new_muted,
            _old_level=old_send.level, _old_enabled=old_send.enabled,
            _old_target_bus_id=old_send.target_bus_id,
            _old_tap=old_send.tap, _old_muted=old_send.muted,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        from src.features.edit.core.timeline_v2 import Send
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        # 还原到旧值（未捕获的旧字段保持当前值）
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
            tap=self._old_tap if self._old_tap is not None else old_send.tap,
            muted=self._old_muted if self._old_muted is not None else old_send.muted,
        )
        return timeline.with_track_updated(track.with_send_updated(restored))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id, "command_type": "UpdateSendCommand",
            "label": self.label, "track_id": self.track_id, "send_id": self.send_id,
            "new_level": self.new_level, "new_enabled": self.new_enabled,
            "new_target_bus_id": self.new_target_bus_id,
            "new_tap": self.new_tap, "new_muted": self.new_muted,
            "old_level": self._old_level, "old_enabled": self._old_enabled,
            "old_target_bus_id": self._old_target_bus_id,
            "old_tap": self._old_tap, "old_muted": self._old_muted,
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
            new_tap=data.get("new_tap"),
            new_muted=data.get("new_muted"),
            _old_level=data.get("old_level"),
            _old_enabled=data.get("old_enabled"),
            _old_target_bus_id=data.get("old_target_bus_id"),
            _old_tap=data.get("old_tap"),
            _old_muted=data.get("old_muted"),
        )

    @staticmethod
    def create(track_id: str, send_id: str, *, level: Optional[float] = None,
               enabled: Optional[bool] = None, target_bus_id: Optional[str] = None,
               tap: Optional[str] = None, muted: Optional[bool] = None,
               ) -> "UpdateSendCommand":
        parts = []
        if level is not None:
            parts.append(f"level={level}")
        if enabled is not None:
            parts.append(f"enabled={enabled}")
        if target_bus_id is not None:
            parts.append(f"target={target_bus_id}")
        if tap is not None:
            parts.append(f"tap={tap}")
        if muted is not None:
            parts.append(f"muted={muted}")
        return UpdateSendCommand(
            command_id=str(uuid.uuid4()),
            label=f"Update Send ({', '.join(parts) or 'no-op'})",
            track_id=track_id, send_id=send_id,
            new_level=level, new_enabled=enabled, new_target_bus_id=target_bus_id,
            new_tap=tap, new_muted=muted,
        )


@dataclass(frozen=True)
class SetTrackSoloSafeCommand:
    """B10：轨道 SIP solo-safe 入时间线历史。

    solo_safe=True 的轨在其他轨 solo 时不被监听闭包静音（控制室监听规则，
    属声音模型字段）。旧工程无该字段时默认 False（迁移零变化）。
    """

    command_id: str
    label: str
    track_id: str
    safe: bool
    _old_safe: bool | None = None

    def apply(self, timeline: TimelineSequence):
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old = bool(track.solo_safe)
        new_timeline = timeline.with_track_updated(
            track.with_solo_safe(bool(self.safe)))
        updated_cmd = SetTrackSoloSafeCommand(
            command_id=self.command_id, label=self.label,
            track_id=self.track_id, safe=bool(self.safe), _old_safe=old)
        logger.debug(
            "SetTrackSoloSafeCommand applied: track %s safe=%s",
            self.track_id, self.safe)
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_safe is None:
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        return timeline.with_track_updated(
            track.with_solo_safe(bool(self._old_safe)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackSoloSafeCommand",
            "label": self.label,
            "track_id": self.track_id,
            "safe": bool(self.safe),
            "old_safe": self._old_safe,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetTrackSoloSafeCommand":
        return SetTrackSoloSafeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Solo Safe"),
            track_id=data.get("track_id", ""),
            safe=bool(data.get("safe", False)),
            _old_safe=data.get("old_safe"),
        )

    @staticmethod
    def create(track_id: str, safe: bool) -> "SetTrackSoloSafeCommand":
        return SetTrackSoloSafeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Solo Safe: {'on' if safe else 'off'}",
            track_id=track_id, safe=bool(safe))
