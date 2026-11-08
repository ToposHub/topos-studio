"""New backend commands participate in the existing macro serialization API."""
import pytest

from src.features.edit.core.commands import (
    MacroCommand, MoveSpeedPointCommand, SetClipSpeedSettingsCommand, SetFadeCurveCommand,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence

pytestmark = pytest.mark.edit_core


@pytest.mark.parametrize('command', [
    MoveSpeedPointCommand.create('c', 'v', 20, 30),
    SetClipSpeedSettingsCommand.create('c', 'v', {'speed': 2, 'duration_frames': 50}),
    SetFadeCurveCommand.create('c', 'v', 'smooth'),
])
def test_new_command_macro_can_roundtrip_before_execution(command) -> None:
    macro = MacroCommand.create('Review roundtrip', [command])
    restored = MacroCommand.from_dict(macro.to_dict())
    assert restored.to_dict() == macro.to_dict()


def test_unapplied_fade_command_roundtrip_cannot_undo_unrelated_curve() -> None:
    clip = ClipInstance('c', 'asset', 'v', 0, 100, 0, 100, fade_curve='linear')
    timeline = TimelineSequence('roundtrip', 'Roundtrip', fps=24,
        video_tracks=(EditTrack('v', 'V1', 'video', 0, clips=(clip,)),))
    command = SetFadeCurveCommand.create('c', 'v', 'smooth')
    restored = SetFadeCurveCommand.from_dict(command.to_dict())
    with pytest.raises(ValueError, match='never applied'):
        restored.undo(timeline)


def test_applied_fade_command_roundtrip_can_restore_legacy_none() -> None:
    clip = ClipInstance('c', 'asset', 'v', 0, 100, 0, 100)
    timeline = TimelineSequence('roundtrip', 'Roundtrip', fps=24,
        video_tracks=(EditTrack('v', 'V1', 'video', 0, clips=(clip,)),))
    command = SetFadeCurveCommand.create('c', 'v', 'smooth')
    changed = command.apply(timeline)
    restored = SetFadeCurveCommand.from_dict(command.to_dict())
    assert restored.undo(changed).get_clip_by_id('c').fade_curve is None
