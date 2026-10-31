"""P1-05 Sync Lock（轨道同步锁）测试。

语义：is_sync_locked=False 的轨道在波纹/提取（Extract）操作中不跟随
位移（该轨退化为 Lift 语义），但范围内的 clip 仍被删除、轨道仍可正常
编辑。默认 True 保持既有行为。与 is_locked（编辑锁）正交。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import (
    MacroCommand,
    SetTrackSyncLockCommand,
)
from src.features.edit.core.timeline_ops import build_range_delete_commands
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int = 100) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id="a1", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
    )


def _timeline(
    video_clips=(), audio_clips=(),
    video_sync_locked=True, audio_sync_locked=True,
) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="tl", name="TL", fps=Rational(24, 1),
        video_tracks=(EditTrack(
            track_id="v1", name="V1", track_type="video",
            clips=tuple(video_clips), is_sync_locked=video_sync_locked,
        ),),
        audio_tracks=(EditTrack(
            track_id="a1", name="A1", track_type="audio",
            clips=tuple(audio_clips), is_sync_locked=audio_sync_locked,
        ),),
    )


class TestModel:
    def test_default_true_and_roundtrip(self):
        track = EditTrack(track_id="t", track_type="video")
        assert track.is_sync_locked is True  # 默认保持既有波纹行为

        track2 = track.with_sync_locked(False)
        assert track2.is_sync_locked is False
        assert track.is_sync_locked is True  # 不可变

        restored = EditTrack.from_dict(track2.to_dict())
        assert restored.is_sync_locked is False
        # 旧工程无该字段 → 默认 True（向后兼容）
        legacy = EditTrack.from_dict(
            {k: v for k, v in track.to_dict().items()
             if k != "is_sync_locked"}
        )
        assert legacy.is_sync_locked is True

    def test_sequence_dict_roundtrip(self):
        tl = _timeline(audio_sync_locked=False)
        data = tl.to_dict()
        restored = TimelineSequence.from_dict(data)
        assert restored.video_tracks[0].is_sync_locked is True
        assert restored.audio_tracks[0].is_sync_locked is False


class TestCommand:
    def test_apply_undo_redo(self):
        tl = _timeline()
        cmd = SetTrackSyncLockCommand.create("a1", False)
        new_tl, updated = cmd.apply(tl)
        assert new_tl.audio_tracks[0].is_sync_locked is False

        undone = updated.undo(new_tl)
        assert undone.audio_tracks[0].is_sync_locked is True

        redone = updated.redo(undone)
        assert redone.audio_tracks[0].is_sync_locked is False

    def test_to_from_dict_roundtrip(self):
        cmd = SetTrackSyncLockCommand.create("v1", False)
        tl, applied = cmd.apply(_timeline())
        data = applied.to_dict()
        assert data["command_type"] == "SetTrackSyncLockCommand"
        restored = SetTrackSyncLockCommand.from_dict(data)
        assert restored.sync_lock is False
        assert restored._old_sync_lock is True

    def test_unknown_track_raises(self):
        with pytest.raises(ValueError):
            SetTrackSyncLockCommand.create("ghost", False).apply(_timeline())


class TestRippleSemantics:
    def _extract(self, tl, track_ids=("v1", "a1"), start=100, end=200):
        cmds = build_range_delete_commands(tl, track_ids, start, end, ripple=True)
        if not cmds:
            return tl
        result = MacroCommand.create("extract", cmds).apply(tl)
        return result[0] if isinstance(result, tuple) else result

    def test_default_all_tracks_ripple(self):
        tl = _timeline(
            video_clips=(_clip("v-a", "v1", 0, 100), _clip("v-b", "v1", 200, 100)),
            audio_clips=(_clip("a-a", "a1", 0, 100), _clip("a-b", "a1", 200, 100)),
        )
        result = self._extract(tl)
        assert [c.timeline_start for c in result.video_tracks[0].clips] == [0, 100]
        assert [c.timeline_start for c in result.audio_tracks[0].clips] == [0, 100]

    def test_sync_lock_off_track_keeps_position(self):
        """关闭同步锁的轨道：范围内 clip 被删，但后续 clip 不左移。"""
        tl = _timeline(
            video_clips=(_clip("v-a", "v1", 0, 100), _clip("v-b", "v1", 200, 100)),
            audio_clips=(_clip("a-a", "a1", 0, 100), _clip("a-b", "a1", 200, 100)),
            audio_sync_locked=False,  # 音乐轨：有意失步
        )
        result = self._extract(tl)
        # 视频轨正常波纹
        assert [c.timeline_start for c in result.video_tracks[0].clips] == [0, 100]
        # 音频轨保持原位（200 不动）—— 与视频轨失步
        assert [c.timeline_start for c in result.audio_tracks[0].clips] == [0, 200]
        # 范围内素材在两轨上都被删除（此例范围 [100,200) 无整段 clip，仅验证语义）
        assert len(result.audio_tracks[0].clips) == 2

    def test_sync_lock_off_deletes_in_range_but_keeps_after(self):
        tl = _timeline(
            video_clips=(_clip("v-in", "v1", 120, 40), _clip("v-after", "v1", 240, 60)),
            audio_clips=(_clip("a-in", "a1", 120, 40), _clip("a-after", "a1", 240, 60)),
            audio_sync_locked=False,
        )
        result = self._extract(tl)
        v_starts = [c.timeline_start for c in result.video_tracks[0].clips]
        a_starts = [c.timeline_start for c in result.audio_tracks[0].clips]
        # 两轨范围内 clip 均被删除
        assert len(v_starts) == 1 and len(a_starts) == 1
        # 视频轨波纹左移 100；音频轨保持 240
        assert v_starts == [140]
        assert a_starts == [240]

    def test_lift_unaffected_by_sync_lock(self):
        """Lift（ripple=False）本来就不移动任何轨道——同步锁无影响。"""
        tl = _timeline(
            video_clips=(_clip("v-in", "v1", 120, 40), _clip("v-after", "v1", 240, 60)),
            audio_clips=(_clip("a-in", "a1", 120, 40), _clip("a-after", "a1", 240, 60)),
            audio_sync_locked=False,
        )
        cmds = build_range_delete_commands(
            tl, ("v1", "a1"), 100, 200, ripple=False
        )
        result = MacroCommand.create("lift", cmds).apply(tl)
        if isinstance(result, tuple):
            result = result[0]
        for track in result.all_tracks:
            starts = sorted(c.timeline_start for c in track.clips)
            assert starts == [240]  # 只删范围内的，其余不动


class TestUiWiring:
    def test_header_menu_emits_sync_lock_signal(self, qapp, monkeypatch):
        """轨道头右键菜单的 Sync Lock 项：checkable + 状态回读 + 发信号。"""
        from PyQt6.QtWidgets import QMenu

        from src.features.edit.ui.timeline_items import TrackHeaderItem

        header = TrackHeaderItem(
            "v1", "V1", 0, "video", is_locked=False, is_sync_locked=True,
        )
        # _show_context_menu 会查 scene().views()（Float/Dock 菜单项）
        from PyQt6.QtWidgets import QGraphicsScene

        scene = QGraphicsScene()
        scene.addItem(header)

        captured = []
        header.sync_lock_changed.connect(lambda tid, v: captured.append((tid, v)))

        # 拦截 QMenu.exec 弹窗，直接驱动菜单构建逻辑
        built: dict = {}

        class _FakeMenu(QMenu):
            def exec(self, *a, **k):
                # 持有菜单引用，避免方法返回后 QAction 被 Qt 回收；
                # 返回 Sync Lock 项（切换选中态）以驱动菜单分发逻辑
                built["menu"] = self
                for act in self.actions():
                    if act.text() in ("Sync Lock", "同步锁"):
                        built["action"] = act
                        act.setChecked(not act.isChecked())  # 模拟用户点击
                        return act
                return None

        # _show_context_menu 内部局部导入 QMenu，补丁 PyQt6.QtWidgets 命名空间
        monkeypatch.setattr("PyQt6.QtWidgets.QMenu", _FakeMenu)
        from PyQt6.QtCore import QPointF

        header._show_context_menu(QPointF(0, 0))

        action = built.get("action")
        assert action is not None, "菜单应包含 Sync Lock 项"
        assert action.isCheckable()
        # exec 已模拟用户点击：True → False，分发读取切换后的状态
        assert action.isChecked() is False
        assert captured == [("v1", False)]
        # T01-b（2026-10-14 复查）：header 明确只 emit 请求、自身不先改
        # 状态（timeline_items 的既定语义——由命令成功后回写，防止
        # UI/model 脱节）。孤立断言旧契约"立即自改 False"已过时。
        assert header._is_sync_locked is True

        # 外部回写（命令成功后）
        header.set_sync_locked(False)
        assert header._is_sync_locked is False

    def test_controller_dispatches_sync_lock_param(self):
        """EditControllerMixin 的 track_param 分发支持 is_sync_locked。"""
        from src.app.controllers.edit_controller import EditControllerMixin
        from src.features.edit.core.commands import SetTrackSyncLockCommand

        source = inspect_source(EditControllerMixin._on_edit_track_param_changed)
        assert "SetTrackSyncLockCommand" in source
        assert '"is_sync_locked"' in source
        assert callable(SetTrackSyncLockCommand.create)


def inspect_source(func) -> str:
    import inspect

    return inspect.getsource(func)
