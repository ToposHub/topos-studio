"""Production model/history regressions found reviewing plan execution."""
from dataclasses import replace

import numpy as np
import pytest

from src.features.edit.core.commands import (
    CommandStack, MacroCommand, SetTrackRoutingCommand, SetTrackVolumeCommand, SetVcaGroupCommand,
)
from src.features.edit.core.timeline_v2 import EditTrack, Send, TimelineSequence, VcaGroup


def sequence() -> TimelineSequence:
    return TimelineSequence(audio_tracks=(
        EditTrack(track_id="dx", track_type="audio", sends=(Send("send", "bus", 0.3, False),)),
        EditTrack(track_id="bus", track_type="audio", is_bus=True),
        EditTrack(track_id="return", track_type="audio", is_bus=True),
    ), vcas=(VcaGroup("vca", "DX", ("dx",), -6.0),))


def test_routing_replaces_all_fields_once_and_undo_retains_identities() -> None:
    original = sequence()
    stack = CommandStack()
    sends = (Send("pre", "return", 0.4, False, "pre_fader", True),
             Send("post", "return", 0.7, True, "post_fader", False))
    command = SetTrackRoutingCommand.create("dx", "bus", sends)
    changed = stack.execute(command, original)
    assert len(stack._undo_stack) == 1
    assert changed.get_track_by_id("dx").sends == sends
    assert changed.vcas == original.vcas
    restored = stack.undo(changed)
    assert restored.to_dict() == original.to_dict()
    assert stack.redo(restored).get_track_by_id("dx").sends == sends
    stored = stack._undo_stack[-1]
    loaded = MacroCommand.from_dict(MacroCommand.create("route", [stored]).to_dict())
    assert loaded.undo(changed).to_dict() == original.to_dict()


@pytest.mark.parametrize("output,sends", [
    ("ghost", ()), ("bus", (Send("bad", "ghost", 0.5),)),
    (None, (Send("bad", "ghost", 0.5, False),)),
    (None, (Send("same", "bus"), Send("same", "return"))),
    (None, (Send("bad", "bus", float("nan")),)),
])
def test_invalid_route_has_no_history_or_model_changes(output, sends) -> None:
    original = sequence()
    stack = CommandStack()
    with pytest.raises(ValueError):
        stack.execute(SetTrackRoutingCommand.create("dx", output, sends), original)
    assert not stack._undo_stack
    assert original.get_track_by_id("dx").output_bus_id is None


def test_bus_cycle_rejected_before_transaction() -> None:
    original = sequence()
    stack = CommandStack()
    routed = stack.execute(SetTrackRoutingCommand.create("bus", "return", ()), original)
    with pytest.raises(ValueError, match="Invalid routing"):
        stack.execute(SetTrackRoutingCommand.create("return", "bus", ()), routed)
    assert len(stack._undo_stack) == 1
    assert routed.get_track_by_id("return").output_bus_id is None


def test_vcas_survive_unrelated_edits_and_save_reopen_then_modify() -> None:
    original = TimelineSequence.from_dict(sequence().to_dict())
    assert "vcas" not in original._extra_fields
    stack = CommandStack()
    changed = stack.execute(SetTrackVolumeCommand.create("dx", 0.4), original)
    assert changed.vcas == original.vcas
    updated = replace(original.vcas[0], gain_db=-12.0)
    changed = stack.execute(SetVcaGroupCommand.create("vca", updated), changed)
    reopened = TimelineSequence.from_dict(changed.to_dict())
    assert reopened.vcas == (updated,)
    assert stack.undo(changed).vcas == original.vcas
    assert stack.redo(stack.undo(changed)).vcas == (updated,)


def test_vca_delete_serialize_undo_and_noop_history() -> None:
    original = sequence()
    stack = CommandStack()
    same = stack.execute(SetVcaGroupCommand.create("vca", original.vcas[0]), original)
    assert same is original and not stack._undo_stack
    deleted = stack.execute(SetVcaGroupCommand.create("vca", None), original)
    assert not deleted.vcas
    command = SetVcaGroupCommand.from_dict(stack._undo_stack[-1].to_dict())
    assert command.undo(deleted).vcas == original.vcas


@pytest.mark.parametrize("group", [VcaGroup("vca", member_track_ids=("ghost",)),
                                      VcaGroup("vca", gain_db=float("nan")),
                                      VcaGroup("other"),
                                      VcaGroup("vca", member_track_ids=("dx", "dx"))])
def test_vca_invalid_edit_rejected(group) -> None:
    with pytest.raises(ValueError):
        SetVcaGroupCommand.create("vca", group).apply(sequence())


@pytest.mark.parametrize("delay", [1, 7, 256, 20000])
def test_pdc_ring_matches_absolute_delay_with_variable_blocks_and_stable_storage(delay: int) -> None:
    from src.features.edit.core.audio_dsp import PdcDelayLine, apply_pdc_delay_line

    rng = np.random.default_rng(77)
    signal = rng.normal(size=(2, 30000)).astype(np.float32)
    original = signal.copy()
    sizes = (3, 1024, 1, 2001, 256, 11, 9000, 17, 512)
    state = {}
    parts = []
    offset = 0
    storage = None
    while offset < signal.shape[-1]:
        size = sizes[len(parts) % len(sizes)]
        block = signal[:, offset:offset + size]
        parts.append(apply_pdc_delay_line(state, "dry", delay, block))
        assert isinstance(state["dry"], PdcDelayLine)
        if storage is None:
            storage = state["dry"].buffer
        assert state["dry"].buffer is storage
        assert storage.nbytes == 2 * delay * 4
        offset += block.shape[-1]
    expected = np.concatenate((np.zeros((2, delay), np.float32), original), axis=1)[:, :30000]
    np.testing.assert_array_equal(np.concatenate(parts, axis=1), expected)
    np.testing.assert_array_equal(signal, original)
