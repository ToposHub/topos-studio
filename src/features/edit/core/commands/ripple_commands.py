"""Insert / ripple-delete commands (P6 split, moved verbatim).

Extracted from commands/__init__.py: insert_ripple cluster remainder —
atomic A/V insertion (plain + ripple), ripple delete, range ripple
delete, batch delete, and append-to-end. RippleDeleteCommand and
BatchDeleteCommand shift neighbors via MoveClipCommand imported from
clip_commands; the shared snapshot deserializers
(_clips_from_dicts / _transitions_from_dicts) and the track-lock guard
assert_track_editable live in _support.py. This module must never
import the infra stacks (MacroCommand / CommandStack) — the dependency
direction is _support -> clip_commands -> ripple, with the package
__init__ re-exporting for deep imports. Class sources are byte-identical
to the original module; to_dict/from_dict keys are a persistence
contract — do not rename.
"""


from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
import logging
import uuid

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
    TimelineMarker,
)

from ._support import _clips_from_dicts, _transitions_from_dicts, assert_track_editable
from .clip_commands import MoveClipCommand

logger = logging.getLogger(__name__)


# ================================================================
# InsertLinkedAssetCommand - Atomic A/V Insertion
# ================================================================

@dataclass(frozen=True)
class InsertLinkedAssetCommand:
    """
    P3: Atomically insert a video clip and its linked audio clip.

    Guarantees:
    - Both clips start at the same timeline_start (no A/V desync).
    - If the audio target track has overlap at that position, a new audio
      track is created automatically so the audio clip still lands at
      timeline_start.
    - If either clip cannot be placed, the entire command fails (no
      half-finished state).
    - Single undo removes both clips.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        video_track_id: Target video track
        video_clip: Video ClipInstance to add
        audio_track_id: Target audio track (existing or newly created)
        audio_clip: Audio ClipInstance to add (None if asset has no audio)
        _created_audio_track_id: ID of auto-created audio track (set during apply)
    """

    command_id: str
    label: str
    video_track_id: str
    video_clip: ClipInstance
    audio_track_id: str | None = None
    audio_clip: ClipInstance | None = None
    _created_audio_track_id: str | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply: insert both video and audio clips atomically."""
        # 1. Validate video track exists
        video_track = timeline.get_track_by_id(self.video_track_id)
        if video_track is None:
            raise ValueError(f"Video track not found: {self.video_track_id}")

        # 2. Check video clip overlap
        for existing in video_track.clips:
            if (self.video_clip.timeline_start < existing.timeline_end and
                    self.video_clip.timeline_end > existing.timeline_start):
                raise ValueError(
                    f"Video clip overlaps with {existing.clip_id} "
                    f"at [{existing.timeline_start}, {existing.timeline_end})"
                )

        # 3. Determine audio track
        audio_tid = self.audio_track_id
        created_track_id: str | None = None

        if self.audio_clip is not None:
            if audio_tid is not None:
                audio_track = timeline.get_track_by_id(audio_tid)
            else:
                audio_track = None

            # A video-only timeline still needs a real audio track before the
            # linked audio clip can be inserted.  Create it inside this
            # atomic command so a later failure leaves no half-created track.
            if audio_track is None:
                new_index = len(timeline.audio_tracks)
                created_track_id = str(uuid.uuid4())
                audio_track = EditTrack(
                    track_id=created_track_id,
                    name=f"A{new_index + 1}",
                    track_type="audio",
                    index=new_index,
                    clips=(),
                    transitions=(),
                    is_enabled=True,
                    is_locked=False,
                    is_muted=False,
                    is_solo=False,
                    volume=1.0,
                )
                timeline = timeline.with_track_added(audio_track)
                audio_tid = created_track_id

            if audio_track is not None and created_track_id is None:
                # Check for overlap on the audio track
                has_overlap = False
                for existing in audio_track.clips:
                    if (self.audio_clip.timeline_start < existing.timeline_end and
                            self.audio_clip.timeline_end > existing.timeline_start):
                        has_overlap = True
                        break

                if has_overlap:
                    # Create a new audio track to avoid overlap
                    new_index = len(timeline.audio_tracks)
                    created_track_id = str(uuid.uuid4())
                    new_audio_track = EditTrack(
                        track_id=created_track_id,
                        name=f"A{new_index + 1}",
                        track_type="audio",
                        index=new_index,
                        clips=(),
                        transitions=(),
                        is_enabled=True,
                        is_locked=False,
                        is_muted=False,
                        is_solo=False,
                        volume=1.0,
                    )
                    timeline = timeline.with_track_added(new_audio_track)
                    audio_tid = created_track_id

        # 4. Apply video clip
        video_clip_with_track = self.video_clip.with_track(self.video_track_id)
        new_video_track = video_track if audio_tid is None or audio_tid != created_track_id else timeline.get_track_by_id(self.video_track_id)
        if new_video_track is None:
            new_video_track = video_track
        new_video_track = new_video_track.with_clip_added(video_clip_with_track)
        timeline = timeline.with_track_updated(new_video_track)

        # 5. Apply audio clip (if present)
        if self.audio_clip is not None and audio_tid is not None:
            audio_clip_with_track = self.audio_clip.with_track(audio_tid)
            target_audio = timeline.get_track_by_id(audio_tid)
            if target_audio is not None:
                target_audio = target_audio.with_clip_added(audio_clip_with_track)
                timeline = timeline.with_track_updated(target_audio)

        # Store created track id for undo
        updated_cmd = InsertLinkedAssetCommand(
            command_id=self.command_id,
            label=self.label,
            video_track_id=self.video_track_id,
            video_clip=self.video_clip,
            audio_track_id=audio_tid,
            audio_clip=self.audio_clip,
            _created_audio_track_id=created_track_id,
        )

        logger.debug(
            f"InsertLinkedAssetCommand applied: video={self.video_clip.clip_id}, "
            f"audio={self.audio_clip.clip_id if self.audio_clip else 'N/A'}"
        )
        return timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo: remove both clips (and auto-created track)."""
        # Remove audio clip first
        if self.audio_clip is not None:
            timeline = timeline.with_clip_removed(self.audio_clip.clip_id)

        # Remove video clip
        timeline = timeline.with_clip_removed(self.video_clip.clip_id)

        # Remove auto-created audio track if we created one
        if self._created_audio_track_id is not None:
            track = timeline.get_track_by_id(self._created_audio_track_id)
            if track is not None and len(track.clips) == 0:
                # Safe to remove empty track we created.
                # AUD-16200: use the immutable rebuilder instead of a manual
                # TimelineSequence(...) constructor — the constructor would
                # silently drop every field added after the original nine
                # (subtitle_tracks, master_*, caption bindings, revision, ...).
                new_audio = tuple(
                    t for t in timeline.audio_tracks
                    if t.track_id != self._created_audio_track_id
                )
                timeline = timeline._rebuild(audio_tracks=new_audio)

        logger.debug(
            f"InsertLinkedAssetCommand undone: video={self.video_clip.clip_id}"
        )
        return timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "InsertLinkedAssetCommand",
            "label": self.label,
            "video_track_id": self.video_track_id,
            "video_clip": self.video_clip.to_dict(),
            "audio_track_id": self.audio_track_id,
            "audio_clip": self.audio_clip.to_dict() if self.audio_clip else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> InsertLinkedAssetCommand:
        """Deserialize command."""
        return InsertLinkedAssetCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Insert Linked Asset"),
            video_track_id=data.get("video_track_id", ""),
            video_clip=ClipInstance.from_dict(data.get("video_clip", {})),
            audio_track_id=data.get("audio_track_id"),
            audio_clip=ClipInstance.from_dict(data["audio_clip"]) if data.get("audio_clip") else None,
        )

    @staticmethod
    def create(
        video_track_id: str,
        video_clip: ClipInstance,
        audio_track_id: str | None = None,
        audio_clip: ClipInstance | None = None,
    ) -> InsertLinkedAssetCommand:
        """Factory method to create atomic A/V insertion command."""
        return InsertLinkedAssetCommand(
            command_id=str(uuid.uuid4()),
            label=f"Insert Linked Asset at {video_clip.timeline_start}",
            video_track_id=video_track_id,
            video_clip=video_clip,
            audio_track_id=audio_track_id,
            audio_clip=audio_clip,
        )


@dataclass(frozen=True)
class InsertLinkedAssetRippleCommand:
    """R22/F-R03：源监视器 A/V 波纹插入（播放头落在片段内部也成立）。

    拖放语义的 :class:`InsertLinkedAssetCommand` 拒绝与既有片段重叠；
    源监视器 Insert 在播放头位于片段内部时此前被吞成 video-only 回退
    （新素材无音频）。本命令在 V/A 两条目标轨做**同步切分+波纹**：

    1. 统一 placement plan（``timeline_duration_for_source`` 一次求
       delta，V/A/其余同步锁轨共用，混合帧率不再分叉）；
    2. 两条目标轨各自经 ``insert_clip_at_playhead`` 切分跨越播放头的
       片段并右移后续片段；
    3. 新 V/A 片段同 playhead 同时长，建立双向链接；
    4. 其余同步锁轨道只做一次统一位移（两条独立 InsertClipCommand 会
       互相重复位移）；
    5. 双轨完整 before/after 快照（含转场），单步 Undo 原子恢复。
    """

    command_id: str
    label: str
    asset_id: str
    in_frame: int
    out_frame: int
    playhead: int
    video_track_id: str
    audio_track_id: str | None = None
    speed: float = 1.0
    _source_fps_num: int | None = None
    _source_fps_den: int | None = None
    # W07 修复：apply() 的 fps_kwargs 引用本字段但 dataclass 从未声明
    # （任何 A/V 波纹插入在 apply 即 AttributeError——路径完全不可用，
    # 2026-09-20 全绿审计后的回归）。与 OverwriteClipCommand 同默认。
    _source_pts_index: tuple | None = None
    _timeline_fps_num: int | None = None
    _timeline_fps_den: int | None = None
    _created_audio_track_id: str | None = None
    _video_clip_id: str | None = None
    _audio_clip_id: str | None = None
    _video_before_clips: tuple[ClipInstance, ...] | None = None
    _video_before_transitions: tuple = None
    _audio_before_clips: tuple[ClipInstance, ...] | None = None
    _audio_before_transitions: tuple = None
    # N06/C3-06：after 快照供 redo 稳定回放（ID 不再随机重生成）
    _video_after_clips: tuple[ClipInstance, ...] | None = None
    _video_after_transitions: tuple = None
    _audio_after_clips: tuple[ClipInstance, ...] | None = None
    _audio_after_transitions: tuple = None
    # N07/C3-07：序列 marker before 快照——切分把 scoped marker 改挂右
    # 残片新 ID 后，undo 只回 clips 会留孤儿 marker
    _before_markers: tuple = None
    _sync_lock_moves: tuple[dict[str, Any], ...] = ()

    def apply(
        self, timeline: TimelineSequence
    ) -> "tuple[TimelineSequence, InsertLinkedAssetRippleCommand]":
        import copy as _copy

        from src.features.edit.core.timeline_ops import (
            insert_clip_at_playhead,
            shift_sync_lock_followers,
            timeline_duration_for_source,
        )

        # R01：任一目标轨锁定即整组拒绝（锁 A 不允许只落 V 留失步对白）
        assert_track_editable(timeline, self.video_track_id, "Insert clip (A/V)")
        if self.audio_track_id:
            assert_track_editable(
                timeline, self.audio_track_id, "Insert clip (A/V)"
            )

        video_track = timeline.get_track_by_id(self.video_track_id)
        if video_track is None:
            raise ValueError(f"Video track not found: {self.video_track_id}")

        # N06/C3-06 redo 快路径：after 快照存在说明已成功 apply 过，直
        # 接回放最终状态——重新走 insert_clip_at_playhead 会重新随机生
        # 成切分/新片段 ID，redo 前后 ID 集合不稳定。
        if self._video_after_clips is not None:
            if (self._created_audio_track_id is not None
                    and timeline.get_track_by_id(
                        self._created_audio_track_id) is None):
                new_index = len(timeline.audio_tracks)
                timeline = timeline.with_track_added(EditTrack(
                    track_id=self._created_audio_track_id,
                    name=f"A{new_index + 1}",
                    track_type="audio",
                    index=new_index,
                ))
            if self.audio_track_id:
                audio_track_after = timeline.get_track_by_id(
                    self.audio_track_id)
                if (audio_track_after is not None
                        and self._audio_after_clips is not None):
                    timeline = timeline.with_track_updated(
                        audio_track_after._rebuild(
                            clips=self._audio_after_clips,
                            transitions=self._audio_after_transitions,
                        )
                    )
            video_track_after = timeline.get_track_by_id(
                self.video_track_id)
            if video_track_after is not None:
                timeline = timeline.with_track_updated(
                    video_track_after._rebuild(
                        clips=self._video_after_clips,
                        transitions=self._video_after_transitions,
                    )
                )
            if self._sync_lock_moves:
                from src.features.edit.core.timeline_ops import (
                    reapply_sync_lock_shifts,
                )
                timeline = reapply_sync_lock_shifts(
                    timeline, self._sync_lock_moves
                )
            return timeline, self

        # 音轨缺失时在命令内原子补建（undo 一并移除）
        audio_tid = self.audio_track_id
        created_track_id = None
        audio_track = (
            timeline.get_track_by_id(audio_tid) if audio_tid else None
        )
        if audio_track is None:
            new_index = len(timeline.audio_tracks)
            created_track_id = str(uuid.uuid4())
            audio_track = EditTrack(
                track_id=created_track_id,
                name=f"A{new_index + 1}",
                track_type="audio",
                index=new_index,
                clips=(),
                transitions=(),
                is_enabled=True,
                is_locked=False,
                is_muted=False,
                is_solo=False,
                volume=1.0,
            )
            timeline = timeline.with_track_added(audio_track)
            audio_tid = created_track_id

        video_before_clips = _copy.deepcopy(video_track.clips)
        video_before_transitions = _copy.deepcopy(video_track.transitions)
        audio_before_clips = _copy.deepcopy(audio_track.clips)
        audio_before_transitions = _copy.deepcopy(audio_track.transitions)
        before_markers = _copy.deepcopy(timeline.markers)
        video_ids_before = {c.clip_id for c in video_before_clips}
        audio_ids_before = {c.clip_id for c in audio_before_clips}

        fps_kwargs = dict(
            source_fps_num=self._source_fps_num,
            source_fps_den=self._source_fps_den,
            timeline_fps_num=self._timeline_fps_num,
            timeline_fps_den=self._timeline_fps_den,
            source_pts_index=self._source_pts_index,
        )

        # 两条目标轨同一 placement（同一时长/切分语义）
        timeline = insert_clip_at_playhead(
            timeline, self.asset_id, self.in_frame, self.out_frame,
            self.playhead, self.video_track_id, self.speed, **fps_kwargs,
        )
        timeline = insert_clip_at_playhead(
            timeline, self.asset_id, self.in_frame, self.out_frame,
            self.playhead, audio_tid, self.speed, **fps_kwargs,
        )

        source_duration = self.out_frame - self.in_frame + 1
        duration = timeline_duration_for_source(
            source_duration, self.speed,
            self._source_fps_num, self._source_fps_den,
            self._timeline_fps_num, self._timeline_fps_den,
        )

        # 识别两条轨的新片段并建立双向链接
        def _find_new(track_id, ids_before):
            track = timeline.get_track_by_id(track_id)
            for clip in track.clips:
                if (
                    clip.clip_id not in ids_before
                    and clip.timeline_start == self.playhead
                    and clip.asset_id == self.asset_id
                ):
                    return clip.clip_id
            return None

        video_clip_id = _find_new(self.video_track_id, video_ids_before)
        audio_clip_id = _find_new(audio_tid, audio_ids_before)
        if video_clip_id and audio_clip_id:
            v_clip = timeline.get_clip_by_id(video_clip_id)
            a_clip = timeline.get_clip_by_id(audio_clip_id)
            timeline = timeline.with_clip_updated(
                v_clip.with_linked_clip_id(audio_clip_id)
            )
            timeline = timeline.with_clip_updated(
                a_clip.with_linked_clip_id(video_clip_id)
            )

        # N07/C3-07：既有 A/V pair 被同一 seam 双轨切分后，两个右残片互
        # 为 reciprocal。此前右残片复制原 linked_clip_id 仍指左伙伴（非
        # 互逆，validator 报 link not reciprocal；save/load normalization
        # 清掉错误链接造成数据丢失）。条件：两条轨恰有一个跨点片段、且
        # 它们原本互为 reciprocal。
        def _crossing(track_obj):
            return [
                c for c in track_obj.clips
                if c.timeline_start < self.playhead < c.timeline_end
            ]

        v_crossing = _crossing(video_track)
        a_crossing = _crossing(audio_track)
        if (len(v_crossing) == 1 and len(a_crossing) == 1
                and v_crossing[0].linked_clip_id == a_crossing[0].clip_id
                and a_crossing[0].linked_clip_id == v_crossing[0].clip_id):
            def _right_residual(track_id, ids_before, inserted_id):
                track_after = timeline.get_track_by_id(track_id)
                residual = [
                    c for c in track_after.clips
                    if c.clip_id not in ids_before
                    and c.clip_id != inserted_id
                ]
                return residual[0] if len(residual) == 1 else None

            v_right = _right_residual(
                self.video_track_id, video_ids_before, video_clip_id)
            a_right = _right_residual(
                audio_tid, audio_ids_before, audio_clip_id)
            if v_right is not None and a_right is not None:
                timeline = timeline.with_clip_updated(
                    v_right.with_linked_clip_id(a_right.clip_id)
                )
                timeline = timeline.with_clip_updated(
                    a_right.with_linked_clip_id(v_right.clip_id)
                )

        # 其余同步锁轨道一次统一位移（排除两条目标轨避免双重位移）
        timeline, sync_moves = shift_sync_lock_followers(
            timeline,
            exclude_track_ids=[self.video_track_id, audio_tid],
            from_frame=self.playhead,
            delta=duration,
        )

        updated = InsertLinkedAssetRippleCommand(
            command_id=self.command_id,
            label=self.label,
            asset_id=self.asset_id,
            in_frame=self.in_frame,
            out_frame=self.out_frame,
            playhead=self.playhead,
            video_track_id=self.video_track_id,
            audio_track_id=audio_tid,
            speed=self.speed,
            _source_fps_num=self._source_fps_num,
            _source_fps_den=self._source_fps_den,
            _timeline_fps_num=self._timeline_fps_num,
            _timeline_fps_den=self._timeline_fps_den,
            _created_audio_track_id=created_track_id,
            _video_clip_id=video_clip_id,
            _audio_clip_id=audio_clip_id,
            _video_before_clips=video_before_clips,
            _video_before_transitions=video_before_transitions,
            _audio_before_clips=audio_before_clips,
            _audio_before_transitions=audio_before_transitions,
            _before_markers=before_markers,
            _video_after_clips=(
                timeline.get_track_by_id(self.video_track_id).clips
                if timeline.get_track_by_id(self.video_track_id) else None
            ),
            _video_after_transitions=(
                timeline.get_track_by_id(self.video_track_id).transitions
                if timeline.get_track_by_id(self.video_track_id) else None
            ),
            _audio_after_clips=(
                timeline.get_track_by_id(audio_tid).clips
                if timeline.get_track_by_id(audio_tid) else None
            ),
            _audio_after_transitions=(
                timeline.get_track_by_id(audio_tid).transitions
                if timeline.get_track_by_id(audio_tid) else None
            ),
            _sync_lock_moves=sync_moves,
        )
        return timeline, updated

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        from src.features.edit.core.timeline_ops import restore_sync_lock_shifts

        if self._sync_lock_moves:
            timeline = restore_sync_lock_shifts(
                timeline, self._sync_lock_moves
            )
        # N07/C3-07：marker 归属随 before 快照复原（右残片 ID 被删）
        if self._before_markers is not None:
            timeline = timeline._rebuild(markers=self._before_markers)
        if self._video_before_clips is not None:
            track = timeline.get_track_by_id(self.video_track_id)
            if track is not None:
                timeline = timeline.with_track_updated(
                    track._rebuild(
                        clips=self._video_before_clips,
                        transitions=self._video_before_transitions,
                    )
                )
        if self._audio_before_clips is not None and self.audio_track_id:
            track = timeline.get_track_by_id(self.audio_track_id)
            if track is not None:
                timeline = timeline.with_track_updated(
                    track._rebuild(
                        clips=self._audio_before_clips,
                        transitions=self._audio_before_transitions,
                    )
                )
        if self._created_audio_track_id is not None:
            track = timeline.get_track_by_id(self._created_audio_track_id)
            if track is not None and not track.clips:
                timeline = timeline._rebuild(
                    audio_tracks=tuple(
                        t for t in timeline.audio_tracks
                        if t.track_id != self._created_audio_track_id
                    )
                )
        return timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "InsertLinkedAssetRippleCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "in_frame": self.in_frame,
            "out_frame": self.out_frame,
            "playhead": self.playhead,
            "video_track_id": self.video_track_id,
            "audio_track_id": self.audio_track_id,
            "speed": self.speed,
            "source_fps_num": self._source_fps_num,
            "source_fps_den": self._source_fps_den,
            "timeline_fps_num": self._timeline_fps_num,
            "timeline_fps_den": self._timeline_fps_den,
            # N06/C3-06：完整 before/after、created IDs、sync 变换随公开
            # 载荷往返——此前只序列化输入参数，JSON 回放的命令没有 Undo
            # 能力（undo 后仍留 6 个 clips）。
            "created_audio_track_id": self._created_audio_track_id,
            "video_clip_id": self._video_clip_id,
            "audio_clip_id": self._audio_clip_id,
            "sync_lock_moves": list(self._sync_lock_moves),
            "video_before_clips": [c.to_dict() for c in self._video_before_clips] if self._video_before_clips else None,
            "video_before_transitions": [t.to_dict() for t in self._video_before_transitions] if self._video_before_transitions else None,
            "audio_before_clips": [c.to_dict() for c in self._audio_before_clips] if self._audio_before_clips else None,
            "audio_before_transitions": [t.to_dict() for t in self._audio_before_transitions] if self._audio_before_transitions else None,
            "video_after_clips": [c.to_dict() for c in self._video_after_clips] if self._video_after_clips else None,
            "video_after_transitions": [t.to_dict() for t in self._video_after_transitions] if self._video_after_transitions else None,
            "audio_after_clips": [c.to_dict() for c in self._audio_after_clips] if self._audio_after_clips else None,
            "audio_after_transitions": [t.to_dict() for t in self._audio_after_transitions] if self._audio_after_transitions else None,
            "before_markers": [m.to_dict() for m in self._before_markers] if self._before_markers else None,
        }

    @staticmethod
    def from_dict(data):
        return InsertLinkedAssetRippleCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Insert Linked Asset (ripple)"),
            asset_id=data.get("asset_id", ""),
            in_frame=int(data.get("in_frame", 0)),
            out_frame=int(data.get("out_frame", 0)),
            playhead=int(data.get("playhead", 0)),
            video_track_id=data.get("video_track_id", ""),
            audio_track_id=data.get("audio_track_id"),
            speed=float(data.get("speed", 1.0)),
            _source_fps_num=data.get("source_fps_num"),
            _source_fps_den=data.get("source_fps_den"),
            _timeline_fps_num=data.get("timeline_fps_num"),
            _timeline_fps_den=data.get("timeline_fps_den"),
            _created_audio_track_id=data.get("created_audio_track_id"),
            _video_clip_id=data.get("video_clip_id"),
            _audio_clip_id=data.get("audio_clip_id"),
            _sync_lock_moves=tuple(data.get("sync_lock_moves", [])),
            _video_before_clips=_clips_from_dicts(data.get("video_before_clips")),
            _video_before_transitions=_transitions_from_dicts(data.get("video_before_transitions")),
            _audio_before_clips=_clips_from_dicts(data.get("audio_before_clips")),
            _audio_before_transitions=_transitions_from_dicts(data.get("audio_before_transitions")),
            _video_after_clips=_clips_from_dicts(data.get("video_after_clips")),
            _video_after_transitions=_transitions_from_dicts(data.get("video_after_transitions")),
            _audio_after_clips=_clips_from_dicts(data.get("audio_after_clips")),
            _audio_after_transitions=_transitions_from_dicts(data.get("audio_after_transitions")),
            _before_markers=tuple(
                TimelineMarker.from_dict(m)
                for m in (data.get("before_markers") or [])
            ),
        )

    @staticmethod
    def create(
        asset_id,
        in_frame,
        out_frame,
        playhead,
        video_track_id,
        audio_track_id=None,
        speed=1.0,
        source_fps_num=None,
        source_fps_den=None,
        timeline_fps_num=None,
        timeline_fps_den=None,
    ):
        return InsertLinkedAssetRippleCommand(
            command_id=str(uuid.uuid4()),
            label=f"Insert Linked Asset (ripple) at {playhead}",
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            playhead=playhead,
            video_track_id=video_track_id,
            audio_track_id=audio_track_id,
            speed=speed,
            _source_fps_num=source_fps_num,
            _source_fps_den=source_fps_den,
            _timeline_fps_num=timeline_fps_num,
            _timeline_fps_den=timeline_fps_den,
        )


# ================================================================
# RippleDeleteCommand - Delete Clip and Close Gap
# ================================================================

@dataclass(frozen=True)
class RippleDeleteCommand:
    """
    Command to delete a clip and close the gap (ripple delete).

    This deletes the clip and moves all subsequent clips on the same track
    forward to fill the gap. If the clip has a linked_clip_id, the linked
    clip on the other track is also ripple-deleted.

    Internally uses timeline_ops.delete_clip(ripple=True) for pure calculation,
    then stores the state changes for undo.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to delete
        _deleted_clip: Deleted clip data for undo
        _moved_clips: Clips that were moved (for undo)
        _linked_track_id: Track of linked clip (if any)
        _linked_clip_id: Linked clip ID (if any)
        _deleted_linked_clip: Deleted linked clip data for undo
        _moved_linked_clips: Clips moved on linked track (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    _deleted_clip: ClipInstance | None = None
    _moved_clips: tuple[dict[str, Any], ...] = ()  # {clip_id, old_start, new_start}
    _linked_track_id: str = ""
    _linked_clip_id: str = ""
    _deleted_linked_clip: ClipInstance | None = None
    _moved_linked_clips: tuple[dict[str, Any], ...] = ()
    _deleted_markers: tuple[TimelineMarker, ...] = ()
    _sync_lock_moves: tuple[dict[str, Any], ...] = ()  # P1-05 跨轨跟随 undo 快照
    # C11：受影响轨道的整轨转场快照（本轨 + 链接轨），undo 末尾精确恢复
    _before_transitions_by_track: dict[str, tuple] = field(default_factory=dict)

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: delete clip and ripple subsequent clips.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip deleted and gap closed
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # N04/C3-04：锁轨拒绝（RippleDelete 是内容写入口，此前可绕锁）
        assert_track_editable(timeline, self.track_id, "Ripple delete")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # C11：删除前采集受影响轨道的转场快照
        transitions_snapshot = {self.track_id: track.transitions}
        if clip.linked_clip_id:
            linked_owner = None
            for t in timeline.all_tracks:
                if any(c.clip_id == clip.linked_clip_id for c in t.clips):
                    linked_owner = t
                    break
            if linked_owner is not None:
                transitions_snapshot[linked_owner.track_id] = linked_owner.transitions

        # Store deleted clip for undo
        deleted_clip = clip
        deleted_markers = tuple(
            marker for marker in timeline.markers
            if marker.clip_id == self.clip_id
        )

        # Find clips after this clip that need to be moved
        clip_duration = clip.timeline_duration
        clip_end_frame = clip.timeline_end

        clips_after = tuple(
            c for c in track.clips
            if c.timeline_start >= clip_end_frame
        )

        # Store move info for undo
        moved_clips_info = tuple(
            {
                "clip_id": c.clip_id,
                "old_start": c.timeline_start,
                "new_start": c.timeline_start - clip_duration,
            }
            for c in clips_after
        )

        # Apply delete with ripple using timeline_ops
        from src.features.edit.core.timeline_ops import delete_clip
        new_timeline = delete_clip(timeline, self.clip_id, ripple=True)

        # Handle linked clip (A/V pair)
        linked_track_id = ""
        linked_clip_id = ""
        deleted_linked_clip = None
        moved_linked_clips_info: tuple[dict[str, Any], ...] = ()

        if timeline.get_reciprocal_linked_clip(deleted_clip) is not None:
            linked_clip = new_timeline.get_clip_by_id(deleted_clip.linked_clip_id)
            if linked_clip is not None:
                linked_clip_id = linked_clip.clip_id
                linked_track = new_timeline.get_track_by_id(linked_clip.track_id)
                if linked_track is not None:
                    linked_track_id = linked_track.track_id
                    deleted_linked_clip = linked_clip
                    deleted_markers += tuple(
                        marker for marker in timeline.markers
                        if marker.clip_id == linked_clip.clip_id
                    )
                    linked_end = linked_clip.timeline_end

                    linked_clips_after = tuple(
                        c for c in linked_track.clips
                        if c.timeline_start >= linked_end
                    )
                    moved_linked_clips_info = tuple(
                        {
                            "clip_id": c.clip_id,
                            "old_start": c.timeline_start,
                            "new_start": c.timeline_start - linked_clip.timeline_duration,
                        }
                        for c in linked_clips_after
                    )

                    new_timeline = delete_clip(
                        new_timeline, linked_clip.clip_id, ripple=True
                    )

        # P1-05 Sync Lock：其余开启同步锁的轨道上，位于被删片段结尾之后
        # 的内容跟随左移（链接轨已由显式删除处理，排除避免双重移动）。
        from src.features.edit.core.timeline_ops import shift_sync_lock_followers
        new_timeline, sync_lock_moves = shift_sync_lock_followers(
            new_timeline,
            exclude_track_ids=[self.track_id, linked_track_id],
            from_frame=deleted_clip.timeline_end,
            delta=-deleted_clip.timeline_duration,
        )

        # Return new timeline and updated command for undo
        updated_cmd = RippleDeleteCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            clip_id=self.clip_id,
            _deleted_clip=deleted_clip,
            _moved_clips=moved_clips_info,
            _linked_track_id=linked_track_id,
            _linked_clip_id=linked_clip_id,
            _deleted_linked_clip=deleted_linked_clip,
            _moved_linked_clips=moved_linked_clips_info,
            _deleted_markers=deleted_markers,
            _sync_lock_moves=sync_lock_moves,
            _before_transitions_by_track=transitions_snapshot,
        )

        logger.debug(
            f"RippleDeleteCommand applied: {self.clip_id} "
            f"(moved {len(clips_after)} clips"
            + (f", linked {linked_clip_id}" if linked_clip_id else "")
            + ")"
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore deleted clip and move clips back.

        Args:
            timeline: Current timeline

        Returns:
            Timeline restored to original state
        """
        # P1-05：先还原跨轨同步锁跟随位移（独立于本轨快照，顺序无关）
        from src.features.edit.core.timeline_ops import restore_sync_lock_shifts
        timeline = restore_sync_lock_shifts(timeline, self._sync_lock_moves)

        # First, move clips back to original positions (reverse order)
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Move clips back (in reverse order to avoid overlap issues)
        current_timeline = timeline
        for move_info in reversed(self._moved_clips):
            clip_id = move_info["clip_id"]
            old_start = move_info["old_start"]

            # Find current clip position
            current_clip = current_timeline.get_clip_by_id(clip_id)
            if current_clip is not None:
                # Move back using MoveClipCommand.apply (not undo)
                # We create a command that moves clip from current pos to original pos
                move_cmd = MoveClipCommand.create(
                    self.track_id,
                    clip_id,
                    old_start,
                    current_clip.timeline_start,
                )
                current_timeline = move_cmd.apply(current_timeline)

        # Then, restore the deleted clip
        if self._deleted_clip is None:
            raise ValueError("Cannot undo: deleted clip not stored")

        # Add clip back using AddClipCommand logic
        track = current_timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        new_track = track.with_clip_added(self._deleted_clip)
        current_timeline = current_timeline.with_track_updated(new_track)

        # Restore linked clip if applicable
        if self._deleted_linked_clip and self._linked_track_id:
            linked_track = current_timeline.get_track_by_id(self._linked_track_id)
            if linked_track is not None:
                # Move linked clips back first
                for move_info in reversed(self._moved_linked_clips):
                    clip_id = move_info["clip_id"]
                    old_start = move_info["old_start"]
                    current_clip = current_timeline.get_clip_by_id(clip_id)
                    if current_clip is not None:
                        move_cmd = MoveClipCommand.create(
                            self._linked_track_id,
                            clip_id,
                            old_start,
                            current_clip.timeline_start,
                        )
                        current_timeline = move_cmd.apply(current_timeline)

                # Re-get track after moves
                linked_track = current_timeline.get_track_by_id(self._linked_track_id)
                if linked_track is not None:
                    new_linked_track = linked_track.with_clip_added(self._deleted_linked_clip)
                    current_timeline = current_timeline.with_track_updated(new_linked_track)

        if self._deleted_markers:
            existing_ids = {marker.marker_id for marker in current_timeline.markers}
            restored_markers = tuple(
                marker for marker in self._deleted_markers
                if marker.marker_id not in existing_ids
            )
            current_timeline = current_timeline._rebuild(
                markers=current_timeline.markers + restored_markers
            )

        # C11：所有片段归位后精确恢复各受影响轨道的转场快照
        for track_id, transitions in self._before_transitions_by_track.items():
            track = current_timeline.get_track_by_id(track_id)
            if track is not None:
                current_timeline = current_timeline.with_track_updated(
                    track._rebuild(transitions=transitions)
                )

        logger.debug(
            f"RippleDeleteCommand undone: restored {self.clip_id} "
            f"and moved {len(self._moved_clips)} clips back"
        )
        return current_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "RippleDeleteCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "deleted_clip": self._deleted_clip.to_dict() if self._deleted_clip else None,
            "moved_clips": list(self._moved_clips),
            "linked_track_id": self._linked_track_id,
            "linked_clip_id": self._linked_clip_id,
            "deleted_linked_clip": (
                self._deleted_linked_clip.to_dict()
                if self._deleted_linked_clip else None
            ),
            "moved_linked_clips": list(self._moved_linked_clips),
            "deleted_markers": [marker.to_dict() for marker in self._deleted_markers],
            "sync_lock_moves": list(self._sync_lock_moves),
            # R09：整轨转场快照往返（C11 载荷补齐）
            "before_transitions_by_track": {
                track_id: [t.to_dict() for t in transitions]
                for track_id, transitions in self._before_transitions_by_track.items()
            },
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RippleDeleteCommand:
        """Deserialize command."""
        deleted_clip_data = data.get("deleted_clip")
        deleted_clip = ClipInstance.from_dict(deleted_clip_data) if deleted_clip_data else None
        linked_clip_data = data.get("deleted_linked_clip")
        deleted_linked_clip = (
            ClipInstance.from_dict(linked_clip_data)
            if linked_clip_data else None
        )

        moved_clips = tuple(data.get("moved_clips", []))
        deleted_markers = tuple(
            TimelineMarker.from_dict(marker)
            for marker in data.get("deleted_markers", [])
        )

        return RippleDeleteCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Ripple Delete"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            _deleted_clip=deleted_clip,
            _moved_clips=moved_clips,
            _linked_track_id=data.get("linked_track_id", ""),
            _linked_clip_id=data.get("linked_clip_id", ""),
            _deleted_linked_clip=deleted_linked_clip,
            _moved_linked_clips=tuple(data.get("moved_linked_clips", [])),
            _deleted_markers=deleted_markers,
            _sync_lock_moves=tuple(data.get("sync_lock_moves", [])),
            _before_transitions_by_track={
                track_id: _transitions_from_dicts(transitions)
                for track_id, transitions in (
                    data.get("before_transitions_by_track") or {}
                ).items()
            },
        )

    @staticmethod
    def create(
        timeline: TimelineSequence,
        track_id: str,
        clip_id: str
    ) -> RippleDeleteCommand:
        """
        Factory method to create ripple delete command.

        Pre-computes the moved clips info by examining the timeline.

        Args:
            timeline: Current timeline (for pre-computing move info)
            track_id: Track containing the clip
            clip_id: Clip ID to delete

        Returns:
            RippleDeleteCommand with stored data for undo
        """
        track = timeline.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")

        clip = track.get_clip_by_id(clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {clip_id}")

        # Pre-compute move info
        clip_duration = clip.timeline_duration
        clip_end_frame = clip.timeline_end

        clips_after = tuple(
            c for c in track.clips
            if c.timeline_start >= clip_end_frame
        )

        moved_clips_info = tuple(
            {
                "clip_id": c.clip_id,
                "old_start": c.timeline_start,
                "new_start": c.timeline_start - clip_duration,
            }
            for c in clips_after
        )

        return RippleDeleteCommand(
            command_id=str(uuid.uuid4()),
            label=f"Ripple Delete {clip_id}",
            track_id=track_id,
            clip_id=clip_id,
            _deleted_clip=clip,
            _moved_clips=moved_clips_info,
            _deleted_markers=tuple(
                marker for marker in timeline.markers
                if marker.clip_id == clip_id
            ),
        )


# ================================================================
# SlideClipCommand - Slide Clip with Adjacent Adjustments (P4.5)
# ================================================================

@dataclass(frozen=True)
class RangeRippleDeleteCommand:
    """M11（S02）：删除片段内的帧区间（词级删除的领域命令）。

    由既有域操作组合实现：``split_clip`` 于区间两端 + ``ripple_delete``
    中段——右段 clip_id 由 split 生成（随机 uuid），经"按起点定位"确定
    性解析，不预测 id。undo 以整轨 clips+transitions 快照精确恢复
    （C11 同模式）。链接 A/V 由计划层对两个片段各发一条命令（单一
    职责；RangeDelete 不自动跟随链接轨）。
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    start_frame: int
    end_frame: int
    # undo 快照（apply 时采集）
    _before_clips: tuple = None
    _before_transitions: tuple = None

    @staticmethod
    def _clip_starting_at(timeline: TimelineSequence,
                          track_id: str, frame: int):
        track = timeline.get_track_by_id(track_id)
        if track is None:
            return None
        for clip in track.clips:
            if clip.timeline_start == frame:
                return clip
        return None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        from src.features.edit.core.timeline_ops import (
            ripple_delete, split_clip,
        )
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        assert_track_editable(timeline, self.track_id, "Range ripple delete")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        start = max(int(self.start_frame), clip.timeline_start)
        end = min(int(self.end_frame), clip.timeline_end)
        if not (clip.timeline_start < start < end <= clip.timeline_end):
            raise ValueError(
                f"删除区间 [{start},{end}) 必须位于片段 "
                f"({clip.timeline_start},{clip.timeline_end}] 内部且非空"
                "——整段删除用 DeleteClipCommand")
        before_clips = track.clips
        before_transitions = track.transitions

        seq = split_clip(timeline, self.clip_id, start)
        piece = self._clip_starting_at(seq, self.track_id, start)
        if piece is None:
            raise ValueError("range delete: 起点定位失败（split 未产生预期段）")
        if end < clip.timeline_end:
            seq = split_clip(seq, piece.clip_id, end)
            piece = self._clip_starting_at(seq, self.track_id, start)
            if piece is None:
                raise ValueError("range delete: 终点定位失败（split 未产生预期段）")
        seq = ripple_delete(seq, piece.clip_id)
        # frozen dataclass：undo 快照经新实例携带
        return seq, RangeRippleDeleteCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            clip_id=self.clip_id,
            start_frame=self.start_frame,
            end_frame=self.end_frame,
            _before_clips=before_clips,
            _before_transitions=before_transitions,
        )

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._before_clips is None:
            raise ValueError("Cannot undo: track snapshot not stored")
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        return timeline.with_track_updated(track._rebuild(
            clips=self._before_clips,
            transitions=(self._before_transitions
                         if self._before_transitions is not None
                         else track.transitions),
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "RangeRippleDeleteCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "start_frame": int(self.start_frame),
            "end_frame": int(self.end_frame),
        }

    @staticmethod
    def create(track_id: str, clip_id: str,
               start_frame: int, end_frame: int,
               label: str = "Range Ripple Delete") -> "RangeRippleDeleteCommand":
        return RangeRippleDeleteCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            track_id=track_id,
            clip_id=clip_id,
            start_frame=int(start_frame),
            end_frame=int(end_frame),
        )


# ================================================================
# BatchDeleteCommand - Delete Multiple Clips
# ================================================================

@dataclass(frozen=True)
class BatchDeleteCommand:
    """
    Command to delete multiple clips at once.

    Supports both normal delete (leave gap) and ripple delete (close gap).

    For ripple delete, each track's clips are processed independently.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        clip_ids: Tuple of clip IDs to delete
        ripple: Whether to close gaps after delete
        _deleted_clips: Deleted clips data for undo
        _moved_clips_per_track: Moved clips per track (for ripple undo)
    """

    command_id: str
    label: str
    clip_ids: tuple[str, ...]
    ripple: bool = False
    _deleted_clips: tuple[ClipInstance, ...] = ()
    _moved_clips_per_track: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)  # track_id -> moved clips
    _deleted_markers: tuple[TimelineMarker, ...] = ()
    _unlinked_partners: tuple[ClipInstance, ...] = ()
    _sync_lock_moves: tuple[dict[str, Any], ...] = ()  # P1-05 跨轨跟随 undo 快照
    # C11：受影响轨道整轨转场快照，undo 末尾精确恢复
    _before_transitions_by_track: dict[str, tuple] = field(default_factory=dict)

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: delete all clips.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clips deleted
        """
        # Store deleted clips and track info for undo
        deleted_clips = []
        moved_clips_per_track: dict[str, list[dict[str, Any]]] = {}
        deleted_ids = set(self.clip_ids)
        # N04/C3-04：任一受影响轨锁定 → 整组拒绝（不半删）
        for affected_track in timeline.video_tracks + timeline.audio_tracks:
            if any(c.clip_id in deleted_ids for c in affected_track.clips):
                assert_track_editable(
                    timeline, affected_track.track_id, "Batch delete")
        unlinked_partners = tuple(
            clip
            for track in timeline.video_tracks + timeline.audio_tracks
            for clip in track.clips
            if clip.clip_id not in deleted_ids
            and clip.linked_clip_id in deleted_ids
        )

        current_timeline = timeline

        # 审计修复 AUD-16202（根因补全）：undo 快照必须在任何删除发生
        # 之前从原始时间线采集。旧写法在删除循环内逐个采集，后续片段
        # 拿到的是已被前一次波纹左移的位置——undo 恢复时与原位片段
        # 重叠（多选同轨波纹删除 undo 中途抛错/留下重叠时间线），且
        # merged_ripple_spans 拿到错误跨度，跨轨同步锁跟随位移也会
        # 算错。与 create() 工厂的预采集语义对齐。
        for clip_id in self.clip_ids:
            clip = timeline.get_clip_by_id(clip_id)
            if clip is None:
                continue

            deleted_clips.append(clip)

        # C03：同轨波纹快照也按原始坐标累计。旧写法对每个删除 span 独立
        # 记 {old, old - 本片时长}，跨两个 span 的 follower 得到两条相互
        # 矛盾的记录（且 new_start 都只减了单个 span 的时长），undo 与
        # 实际几何漂移。改为先把同轨被删跨度合并，再对每条幸存片段求
        # 累计位移，一条片段只存一份最初 before 与最终 after。
        if self.ripple:
            from src.features.edit.core.timeline_ops import merged_ripple_spans

            deleted_by_track: dict[str, list] = {}
            for clip in deleted_clips:
                deleted_by_track.setdefault(clip.track_id, []).append(clip)
            for track_id, removed in deleted_by_track.items():
                spans = merged_ripple_spans(removed)
                if not spans:
                    continue
                track = timeline.get_track_by_id(track_id)
                if track is None:
                    continue
                moved_clips_per_track[track_id] = [
                    {
                        "clip_id": c.clip_id,
                        "old_start": c.timeline_start,
                        "new_start": c.timeline_start - sum(
                            end - start for start, end in spans
                            if c.timeline_start >= end
                        ),
                    }
                    for c in track.clips
                    if c.clip_id not in deleted_ids
                    and any(c.timeline_start >= end for _, end in spans)
                ]

        # C11：删除前采集受影响轨道转场快照（delete_clip 内部
        # with_clip_removed 会顺带删除引用转场）
        transitions_snapshot = {
            tid: timeline.get_track_by_id(tid).transitions
            for tid in sorted({c.track_id for c in deleted_clips})
            if timeline.get_track_by_id(tid) is not None
        }

        # Apply delete
        for clip_id in self.clip_ids:
            if timeline.get_clip_by_id(clip_id) is None:
                continue
            from src.features.edit.core.timeline_ops import delete_clip
            current_timeline = delete_clip(current_timeline, clip_id, ripple=self.ripple)

        # Create updated command for undo storage
        # Note: We'll rely on apply being called once and storing data

        # P1-05 Sync Lock：波纹删除时其余同步锁轨道跟随。删除片段的
        # [start, end) 区间做并集——链接 A/V 对（两条轨同跨度）合并为
        # 一个事件，其他轨道只跟随一次；不同轨不重叠的删除保持独立
        # 事件，下游跟随各自累计。
        sync_lock_moves: tuple[dict[str, Any], ...] = ()
        if self.ripple and deleted_clips:
            from src.features.edit.core.timeline_ops import (
                merged_ripple_spans,
                shift_sync_lock_followers_merged,
            )
            edited_track_ids = sorted({c.track_id for c in deleted_clips})
            # C03：所有删除跨度合并成一次跟随位移（原始坐标系累计 delta，
            # 每条 follower 只记一份 before/after）。旧的逐 span 循环第二
            # 个 span 用原始阈值判断已被第一个 span 移过的片段，跨轨少移。
            current_timeline, sync_lock_moves = shift_sync_lock_followers_merged(
                current_timeline,
                exclude_track_ids=edited_track_ids,
                spans=merged_ripple_spans(deleted_clips),
            )

        deleted_markers = self._deleted_markers or tuple(
            marker for marker in timeline.markers
            if marker.clip_id in self.clip_ids
        )

        updated_command = replace(
            self,
            _deleted_clips=tuple(deleted_clips),
            _moved_clips_per_track={
                track_id: tuple(moved)
                for track_id, moved in moved_clips_per_track.items()
            },
            _deleted_markers=deleted_markers,
            _unlinked_partners=unlinked_partners,
            _sync_lock_moves=sync_lock_moves,
            _before_transitions_by_track=transitions_snapshot,
        )

        logger.debug(
            f"BatchDeleteCommand applied: {len(deleted_clips)} clips deleted"
            f"{', ripple' if self.ripple else ''}"
        )
        return current_timeline, updated_command

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore all deleted clips.

        Args:
            timeline: Current timeline

        Returns:
            Timeline restored to original state
        """
        current_timeline = timeline

        # P1-05：先还原跨轨同步锁跟随位移（独立于本轨快照，顺序无关）
        from src.features.edit.core.timeline_ops import restore_sync_lock_shifts
        current_timeline = restore_sync_lock_shifts(
            current_timeline, self._sync_lock_moves
        )

        if self.ripple:
            # First, move clips back to original positions (per track, reverse order)
            for track_id, moved_clips in self._moved_clips_per_track.items():
                track = current_timeline.get_track_by_id(track_id)
                if track is None:
                    continue

                for move_info in reversed(moved_clips):
                    clip_id = move_info["clip_id"]
                    old_start = move_info["old_start"]

                    current_clip = current_timeline.get_clip_by_id(clip_id)
                    if current_clip is not None:
                        # 审计修复 AUD-16202：create 的第 3 参是 new_start，
                        # 必须用 apply 把片段移回 old_start（RippleDeleteCommand
                        # 同款写法）。此前误调 undo：undo 恢复到 old_timeline_start
                        # （即当前位置），片段回移退化为 no-op，随后恢复被删
                        # 片段会与未移回的片段重叠，导致 undo 中途抛错或留下
                        # 重叠损坏的时间线。
                        move_cmd = MoveClipCommand.create(
                            track_id,
                            clip_id,
                            old_start,
                            current_clip.timeline_start,
                        )
                        current_timeline = move_cmd.apply(current_timeline)

        # Restore deleted clips (in reverse order to avoid overlap)
        for clip in reversed(self._deleted_clips):
            track = current_timeline.get_track_by_id(clip.track_id)
            if track is None:
                continue

            new_track = track.with_clip_added(clip)
            current_timeline = current_timeline.with_track_updated(new_track)

        for partner in self._unlinked_partners:
            if current_timeline.get_clip_by_id(partner.clip_id) is not None:
                current_timeline = current_timeline.with_clip_updated(partner)

        if self._deleted_markers:
            existing_ids = {marker.marker_id for marker in current_timeline.markers}
            restored_markers = tuple(
                marker for marker in self._deleted_markers
                if marker.marker_id not in existing_ids
            )
            current_timeline = current_timeline._rebuild(
                markers=current_timeline.markers + restored_markers
            )

        # C11：片段全部归位后精确恢复各轨道转场快照
        for track_id, transitions in self._before_transitions_by_track.items():
            track = current_timeline.get_track_by_id(track_id)
            if track is not None:
                current_timeline = current_timeline.with_track_updated(
                    track._rebuild(transitions=transitions)
                )

        logger.debug(
            f"BatchDeleteCommand undone: restored {len(self._deleted_clips)} clips"
        )
        return current_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        deleted_clips_dicts = [c.to_dict() for c in self._deleted_clips]

        moved_clips_serialized = {}
        for track_id, moved_list in self._moved_clips_per_track.items():
            moved_clips_serialized[track_id] = list(moved_list)

        return {
            "command_id": self.command_id,
            "command_type": "BatchDeleteCommand",
            "label": self.label,
            "clip_ids": list(self.clip_ids),
            "ripple": self.ripple,
            "deleted_clips": deleted_clips_dicts,
            "moved_clips_per_track": moved_clips_serialized,
            "deleted_markers": [marker.to_dict() for marker in self._deleted_markers],
            "unlinked_partners": [clip.to_dict() for clip in self._unlinked_partners],
            "sync_lock_moves": list(self._sync_lock_moves),
            # R09：整轨转场快照往返（C11 载荷补齐）
            "before_transitions_by_track": {
                track_id: [t.to_dict() for t in transitions]
                for track_id, transitions in self._before_transitions_by_track.items()
            },
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> BatchDeleteCommand:
        """Deserialize command."""
        deleted_clips_data = data.get("deleted_clips", [])
        deleted_clips = tuple(
            ClipInstance.from_dict(c) for c in deleted_clips_data
        )

        moved_clips_per_track = {}
        for track_id, moved_list in data.get("moved_clips_per_track", {}).items():
            moved_clips_per_track[track_id] = tuple(moved_list)
        deleted_markers = tuple(
            TimelineMarker.from_dict(marker)
            for marker in data.get("deleted_markers", [])
        )
        unlinked_partners = tuple(
            ClipInstance.from_dict(clip)
            for clip in data.get("unlinked_partners", [])
        )

        return BatchDeleteCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Batch Delete"),
            clip_ids=tuple(data.get("clip_ids", [])),
            ripple=data.get("ripple", False),
            _deleted_clips=deleted_clips,
            _moved_clips_per_track=moved_clips_per_track,
            _deleted_markers=deleted_markers,
            _unlinked_partners=unlinked_partners,
            _sync_lock_moves=tuple(data.get("sync_lock_moves", [])),
            _before_transitions_by_track={
                track_id: _transitions_from_dicts(transitions)
                for track_id, transitions in (
                    data.get("before_transitions_by_track") or {}
                ).items()
            },
        )

    @staticmethod
    def create(
        timeline: TimelineSequence,
        clip_ids: list[str],
        ripple: bool = False
    ) -> BatchDeleteCommand:
        """
        Factory method to create batch delete command.

        Pre-computes all deleted clips and moved clips info.

        Args:
            timeline: Current timeline
            clip_ids: List of clip IDs to delete
            ripple: Whether to close gaps after delete

        Returns:
            BatchDeleteCommand with stored data for undo
        """
        deleted_clips = []
        moved_clips_per_track: dict[str, list[dict[str, Any]]] = {}

        # Sort clips by timeline start for proper processing order
        clips_to_delete = []
        for clip_id in clip_ids:
            clip = timeline.get_clip_by_id(clip_id)
            if clip is not None:
                clips_to_delete.append(clip)

        # Sort by start position (earliest first)
        clips_to_delete.sort(key=lambda c: c.timeline_start)

        for clip in clips_to_delete:
            deleted_clips.append(clip)

            if ripple:
                track = timeline.get_track_by_id(clip.track_id)
                if track is None:
                    continue

                # Calculate which clips will move after deleting this clip
                # Note: This is a simplified calculation; actual ripple is
                # more complex when multiple clips on same track are deleted
                clip_duration = clip.timeline_duration

                # Find clips after this clip that aren't also being deleted
                clips_after = [
                    c for c in track.clips
                    if c.timeline_start >= clip.timeline_end
                    and c.clip_id not in clip_ids
                ]

                track_id = clip.track_id
                if track_id not in moved_clips_per_track:
                    moved_clips_per_track[track_id] = []

                for c in clips_after:
                    moved_clips_per_track[track_id].append({
                        "clip_id": c.clip_id,
                        "old_start": c.timeline_start,
                        "new_start": c.timeline_start - clip_duration,
                    })

        return BatchDeleteCommand(
            command_id=str(uuid.uuid4()),
            label=f"Delete {len(clip_ids)} clips{' (Ripple)' if ripple else ''}",
            clip_ids=tuple(clip_ids),
            ripple=ripple,
            _deleted_clips=tuple(deleted_clips),
            _moved_clips_per_track={k: tuple(v) for k, v in moved_clips_per_track.items()},
            _deleted_markers=tuple(
                marker for marker in timeline.markers
                if marker.clip_id in clip_ids
            ),
        )


# ================================================================
# AppendClipCommand (P3.2)
# ================================================================

@dataclass(frozen=True)
class AppendClipCommand:
    """
    Command to append a clip to the end of timeline.

    Append operation:
    1. Find the end of target track
    2. Add new clip at the end position

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        asset_id: Source asset ID
        in_frame: Source start frame
        out_frame: Source end frame (inclusive)
        track_id: Target track ID
        speed: Playback speed (default 1.0)
        _inserted_clip_id: ID of the inserted clip (set during apply)
        _insert_position: Position where clip was inserted (for undo)
    """

    command_id: str
    label: str
    asset_id: str
    in_frame: int
    out_frame: int
    track_id: str
    speed: float = 1.0
    _inserted_clip_id: str | None = None
    _insert_position: int | None = None
    # R22/F-R03：fps 元数据（混合帧率追加时长统一走 helper）
    _source_fps_num: int | None = None
    _source_fps_den: int | None = None
    # W07 修复：apply() 引用但字段未声明（追加命令 apply 即崩）——
    # 与 Insert/Overwrite 同因同修（pts 时间基快照透传）
    _source_pts_index: tuple | None = None
    _timeline_fps_num: int | None = None
    _timeline_fps_den: int | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: append clip to timeline end.

        Args:
            timeline: Current timeline

        Returns:
            New timeline with clip appended
        """
        from src.features.edit.core.timeline_ops import append_clip_to_timeline

        # N04/C3-04：锁轨拒绝（Append 是内容写入口，此前可绕锁）
        assert_track_editable(timeline, self.track_id, "Append clip")

        # AUD-16212: 用"apply 前后的 clip_id 差集"精确识别新追加的片段。
        # 旧启发式（同 asset 且 start 接近轨道总长）在轨道末尾本就有同
        # 素材旧片段时会认错目标，undo 可能删掉旧片段而留下新片段。
        track_before = timeline.get_track_by_id(self.track_id)
        before_clip_ids = (
            {clip.clip_id for clip in track_before.clips}
            if track_before is not None else set()
        )

        # Apply append operation
        new_timeline = append_clip_to_timeline(
            timeline,
            self.asset_id,
            self.in_frame,
            self.out_frame,
            self.track_id,
            self.speed,
            source_fps_num=self._source_fps_num,
            source_fps_den=self._source_fps_den,
            timeline_fps_num=self._timeline_fps_num,
            timeline_fps_den=self._timeline_fps_den,
            source_pts_index=self._source_pts_index,
        )

        # Find the newly inserted clip（差集中唯一的新 id 即为追加产物）
        new_track = new_timeline.get_track_by_id(self.track_id)
        if new_track is not None:
            added_clips = [
                clip for clip in new_track.clips if clip.clip_id not in before_clip_ids
            ]
            inserted_clip = added_clips[-1] if added_clips else None
            inserted_clip_id = inserted_clip.clip_id if inserted_clip else None
            insert_position = inserted_clip.timeline_start if inserted_clip else None
        else:
            inserted_clip_id = None
            insert_position = None

        # Return updated command with stored values for undo
        return new_timeline, AppendClipCommand(
            command_id=self.command_id,
            label=self.label,
            asset_id=self.asset_id,
            in_frame=self.in_frame,
            out_frame=self.out_frame,
            track_id=self.track_id,
            speed=self.speed,
            _inserted_clip_id=inserted_clip_id,
            _insert_position=insert_position,
            _source_fps_num=self._source_fps_num,
            _source_fps_den=self._source_fps_den,
            _timeline_fps_num=self._timeline_fps_num,
            _timeline_fps_den=self._timeline_fps_den,
        )

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: remove appended clip.

        Args:
            timeline: Current timeline

        Returns:
            Timeline without the appended clip
        """
        if self._inserted_clip_id is None:
            raise ValueError("Cannot undo: inserted clip ID not stored")

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Remove the appended clip
        new_track = track.with_clip_removed(self._inserted_clip_id)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"AppendClipCommand undone: removed {self._inserted_clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "AppendClipCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "in_frame": self.in_frame,
            "out_frame": self.out_frame,
            "track_id": self.track_id,
            "speed": self.speed,
            "inserted_clip_id": self._inserted_clip_id,
            "insert_position": self._insert_position,
            # N06/C3-06：fps 时基字段必须随载荷往返（同 Overwrite）
            "source_fps_num": self._source_fps_num,
            "source_fps_den": self._source_fps_den,
            "timeline_fps_num": self._timeline_fps_num,
            "timeline_fps_den": self._timeline_fps_den,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AppendClipCommand:
        """Deserialize command."""
        return AppendClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Append Clip"),
            asset_id=data.get("asset_id", ""),
            in_frame=data.get("in_frame", 0),
            out_frame=data.get("out_frame", 0),
            track_id=data.get("track_id", ""),
            speed=data.get("speed", 1.0),
            _inserted_clip_id=data.get("inserted_clip_id"),
            _insert_position=data.get("insert_position"),
            _source_fps_num=data.get("source_fps_num"),
            _source_fps_den=data.get("source_fps_den"),
            _timeline_fps_num=data.get("timeline_fps_num"),
            _timeline_fps_den=data.get("timeline_fps_den"),
        )

    @staticmethod
    def create(
        asset_id: str,
        in_frame: int,
        out_frame: int,
        track_id: str,
        speed: float = 1.0,
        source_fps_num: int | None = None,
        source_fps_den: int | None = None,
        timeline_fps_num: int | None = None,
        timeline_fps_den: int | None = None,
    ) -> AppendClipCommand:
        """Factory method to create command."""
        return AppendClipCommand(
            command_id=str(uuid.uuid4()),
            label="Append Clip",
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            track_id=track_id,
            speed=speed,
            _source_fps_num=source_fps_num,
            _source_fps_den=source_fps_den,
            _timeline_fps_num=timeline_fps_num,
            _timeline_fps_den=timeline_fps_den,
        )
