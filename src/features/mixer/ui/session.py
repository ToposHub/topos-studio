"""Transient mixer UI state. Never writes an EditProject or opens audio devices."""
from __future__ import annotations

import copy
import json
import math
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QUndoCommand, QUndoStack

from src.features.mixer.effects import EffectInstance


@dataclass(frozen=True)
class DisplayRate:
    label: str
    numerator: int
    denominator: int
    nominal: int
    drop: int = 0

    @property
    def fps(self) -> float:
        return self.numerator / self.denominator

    def format(self, seconds: float) -> str:
        frame = max(0, math.floor(seconds * self.fps + 1e-7))
        if self.drop:
            block, tail = divmod(frame, self.nominal * 600 - self.drop * 9)
            frame += self.drop * (9 * block + max(0, (tail - self.drop) // (self.nominal * 60 - self.drop)))
        seconds_i, ff = divmod(frame, self.nominal)
        minute, ss = divmod(seconds_i, 60)
        hh, mm = divmod(minute, 60)
        return f"{hh:02d}:{mm:02d}:{ss:02d}{';' if self.drop else ':'}{ff:02d}"

    def parse(self, text: str) -> float:
        fields = text.strip().replace(";", ":").split(":")
        if len(fields) != 4:
            raise ValueError("请输入 HH:MM:SS:FF")
        hh, mm, ss, ff = map(int, fields)
        if hh < 0 or not 0 <= mm < 60 or not 0 <= ss < 60 or not 0 <= ff < self.nominal:
            raise ValueError("时间码超出范围")
        if self.drop and mm % 10 and ss == 0 and ff < self.drop:
            raise ValueError("该 DF 帧编号被跳过")
        minutes = hh * 60 + mm
        frame = (hh * 3600 + mm * 60 + ss) * self.nominal + ff - self.drop * (minutes - minutes // 10)
        return frame / self.fps


DISPLAY_RATES = (
    DisplayRate("23.976 NDF", 24000, 1001, 24), DisplayRate("24 fps", 24, 1, 24),
    DisplayRate("25 fps", 25, 1, 25), DisplayRate("29.97 NDF", 30000, 1001, 30),
    DisplayRate("29.97 DF", 30000, 1001, 30, 2), DisplayRate("30 fps", 30, 1, 30),
    DisplayRate("50 fps", 50, 1, 50), DisplayRate("59.94 NDF", 60000, 1001, 60),
    DisplayRate("59.94 DF", 60000, 1001, 60, 4), DisplayRate("60 fps", 60, 1, 60),
)


@dataclass
class UiTrack:
    id: str
    name: str
    role: str
    color: str
    volume_db: float = 0.0
    pan: float = 0.0
    muted: bool = False
    solo: bool = False
    armed: bool = False
    phase_inverted: bool = False
    automation_mode: str = "Read"
    output: str = "Master"
    send_target: str = "无发送"
    send_db: float = -12.0
    send_pre: bool = False
    trim_db: float = 0.0
    hpf: bool = False
    width: float = 100.0
    eq: tuple[float, float, float] = (0.0, 0.0, 0.0)
    monitor_mono: bool = False
    dim: bool = False
    effects: list[EffectInstance] = field(default_factory=list)


@dataclass
class UiClip:
    id: str
    track_id: str
    name: str
    start: float
    duration: float
    offset: float = 0.0
    gain_db: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    locked: bool = False
    muted: bool = False
    peaks: tuple[float, ...] = ()
    source_duration: float = 0.0
    effects: list[EffectInstance] = field(default_factory=list)


def effect_scope(owner: UiClip | UiTrack) -> str:
    if isinstance(owner, UiClip):
        return "clip"
    return {"BUS": "bus", "MASTER": "master"}.get(owner.role, "track")


class _UiChange(QUndoCommand):
    def __init__(self, session: MixerUiSession, label: str, before: tuple, after: tuple) -> None:
        super().__init__(label)
        self.session, self.before, self.after = session, before, after

    def undo(self) -> None:
        self.session._restore(self.before)

    def redo(self) -> None:
        self.session._restore(self.after)


class MixerUiSession(QObject):
    changed = pyqtSignal()
    selection_changed = pyqtSignal(str)
    playhead_changed = pyqtSignal(float)
    timeline_format_changed = pyqtSignal()
    status_changed = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.undo_stack = QUndoStack(self)
        self.rate = DISPLAY_RATES[1]
        self.time_format = "timecode"
        self.bpm = 120.0
        self.snap_frames = 1
        self.snap_enabled = True
        self.playhead = 0.0
        self.playing = False
        self.loop_enabled = False
        self.loop_start = 0.0
        self.loop_end = 86.913
        self.tracks = [UiTrack("dx", "对白 DX", "DX", "#70C5B4", output="DX Bus"),
                       UiTrack("fx", "音效 FX", "FX", "#DAA36C", output="FX Bus"),
                       UiTrack("mx", "音乐 MX", "MX", "#9F9ADB", output="MX Bus"),
                       UiTrack("dx_bus", "DX Bus", "BUS", "#70C5B4"),
                       UiTrack("fx_bus", "FX Bus", "BUS", "#DAA36C"),
                       UiTrack("mx_bus", "MX Bus", "BUS", "#9F9ADB"),
                       UiTrack("master", "Master", "MASTER", "#8CA6B8")]
        data_path = Path(__file__).parents[1] / "assets" / "reference_peaks.json"
        sources = json.loads(data_path.read_text(encoding="utf-8"))
        roles = {"DX": "dx", "FX": "fx", "MX": "mx"}
        self.clips = [UiClip(f"clip_{source['role']}", roles[source["role"]], source["name"], 0.0,
                             float(source["duration"]), peaks=tuple(float(v) for v in source["peaks"]),
                             source_duration=float(source["duration"]))
                      for source in sources]
        self.selected_clip_id: str | None = "clip_DX"
        self.selected_track_id = "dx"

    @property
    def fps(self) -> float:
        return self.rate.fps

    @property
    def frame_duration(self) -> float:
        return 1.0 / self.fps

    @property
    def duration(self) -> float:
        return max(90.0, max((clip.start + clip.duration for clip in self.clips), default=0.0) + 5.0)

    def timecode(self, seconds: float) -> str:
        if self.time_format == "seconds":
            return f"{seconds:.3f} s"
        if self.time_format == "bars":
            beats = seconds * self.bpm / 60.0
            return f"{int(beats // 4) + 1:03d}.{int(beats % 4) + 1}.{int(beats % 1 * 960):03d}"
        return self.rate.format(seconds)

    def seconds_from_timecode(self, text: str) -> float:
        if ":" in text or ";" in text:
            return self.rate.parse(text)
        seconds = float(text.removesuffix("s").strip())
        if not math.isfinite(seconds):
            raise ValueError("时间超出范围")
        return max(0.0, seconds)

    def set_rate(self, index: int) -> None:
        self.rate = DISPLAY_RATES[index]
        self.timeline_format_changed.emit()
        self.playhead_changed.emit(self.playhead)

    def clip_by_id(self, clip_id: str | None) -> UiClip | None:
        return next((clip for clip in self.clips if clip.id == clip_id), None)

    def track_by_id(self, track_id: str) -> UiTrack | None:
        return next((track for track in self.tracks if track.id == track_id), None)

    def select_clip(self, clip_id: str | None) -> None:
        self.selected_clip_id = clip_id
        clip = self.clip_by_id(clip_id)
        if clip:
            self.selected_track_id = clip.track_id
        self.selection_changed.emit(clip_id or "")

    def select_track(self, track_id: str) -> None:
        self.selected_track_id = track_id
        self.selection_changed.emit(self.selected_clip_id or "")

    def seek(self, seconds: float) -> None:
        self.playhead = max(0.0, min(self.duration, seconds))
        self.playhead_changed.emit(self.playhead)

    def touch(self) -> None:
        self.changed.emit()

    def snapshot(self) -> tuple:
        return copy.deepcopy((self.tracks, self.clips, self.selected_track_id, self.selected_clip_id))

    def _restore(self, snapshot: tuple) -> None:
        self.tracks, self.clips, self.selected_track_id, self.selected_clip_id = copy.deepcopy(snapshot)
        self.changed.emit()
        self.selection_changed.emit(self.selected_clip_id or "")

    def commit(self, label: str, mutate: Callable[[], None]) -> None:
        before = self.snapshot()
        mutate()
        after = self.snapshot()
        if before != after:
            self.undo_stack.push(_UiChange(self, label, before, after))

    def commit_preview(self, label: str, before: tuple) -> None:
        after = self.snapshot()
        if before != after:
            self.undo_stack.push(_UiChange(self, label, before, after))

    def set_track_value(self, track_id: str, key: str, value: object) -> None:
        track = self.track_by_id(track_id)
        if track and key in UiTrack.__dataclass_fields__ and key not in ("id", "effects"):
            def change() -> None:
                if key == "name":
                    old = track.name
                    for other in self.tracks:
                        if other.output == old:
                            other.output = str(value)
                        if other.send_target == old:
                            other.send_target = str(value)
                setattr(track, key, value)
            self.commit(f"修改 {track.name} · {key}", change)

    def add_track(self, name: str = "新音频轨道", role: str = "FX") -> None:
        track = UiTrack(uuid.uuid4().hex[:10], name, role, "#8CA6B8")
        self.commit("添加音频轨道", lambda: self.tracks.insert(max(0, len(self.tracks) - 1), track))

    def delete_clip(self, clip_id: str) -> None:
        clip = self.clip_by_id(clip_id)
        if clip and not clip.locked:
            def remove() -> None:
                self.clips.remove(clip)
                self.selected_clip_id = None
            self.commit("删除片段", remove)

    def duplicate_clip(self, clip_id: str) -> None:
        clip = self.clip_by_id(clip_id)
        if clip:
            other = copy.deepcopy(clip)
            other.id = uuid.uuid4().hex[:10]
            other.start += other.duration
            other.effects = []
            self.commit("复制片段", lambda: self.clips.append(other))
            self.select_clip(other.id)

    def split_clip(self, clip_id: str, seconds: float) -> None:
        clip = self.clip_by_id(clip_id)
        if not clip or clip.locked or not clip.start + self.frame_duration <= seconds <= clip.start + clip.duration - self.frame_duration:
            return
        def split() -> None:
            other = copy.deepcopy(clip)
            other.id = uuid.uuid4().hex[:10]
            other.offset += seconds - clip.start
            other.duration = clip.start + clip.duration - seconds
            other.start = seconds
            other.fade_in = 0.0
            other.effects = []
            clip.duration = seconds - clip.start
            clip.fade_out = 0.0
            self.clips.append(other)
            self.selected_clip_id = other.id
        self.commit("分割片段", split)

    def set_effects(self, owner_id: str, effects: list[EffectInstance]) -> None:
        owner = self.track_by_id(owner_id) or self.clip_by_id(owner_id)
        if owner:
            self.commit("调整效果机架", lambda: setattr(owner, "effects", copy.deepcopy(effects)))

    def import_media_paths(self, paths: list[str]) -> None:
        if paths:
            self.status_changed.emit(f"已选择 {len(paths)} 个素材：解码、波形与工程导入等待后端接线；未创建虚假音频片段。")
