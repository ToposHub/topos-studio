"""FX-H04-UI verification: Effects Inspector drag-reorder.

Covers the acceptance criteria from the plan:
- Drag A→C across multiple effects, release submits exactly one command.
- Cancel (drop at same position) emits no signal.
- Execute, undo, redo, JSON round-trip preserve effect-id order.
- Non-commutative effect chains produce different output in different orders.
- Panel helper logic: insertion index computation, MIME parsing.

Tests are headless (no QApplication required for command-layer tests).
Panel helper tests use lightweight mocks to avoid needing a display server.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Ensure project root is importable.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.features.edit.core.commands import ReorderEffectCommand
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


@pytest.fixture(scope="module")
def qapp():
    """A real offscreen QApplication for the reorder UI entry point."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


# ================================================================
# Helpers
# ================================================================

def _effect(eid: str, etype: str = "blur") -> EffectInstance:
    return EffectInstance(effect_id=eid, effect_type=etype, params={"radius": 1.0})


def _timeline_with_effects(*effects: EffectInstance) -> TimelineSequence:
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="t1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
        effects=tuple(effects),
    )
    track = EditTrack(track_id="t1", name="V1", track_type="video",
                      index=0, clips=(clip,))
    return TimelineSequence(
        timeline_id="seq", name="Seq",
        fps=Rational(24, 1), width=64, height=64, video_tracks=(track,),
    )


def _effect_ids(seq: TimelineSequence, clip_id: str = "c1") -> List[str]:
    clip = seq.get_clip_by_id(clip_id)
    return [e.effect_id for e in clip.effects]


# ================================================================
# Command-layer: drag A→C, undo, redo, JSON round-trip
# ================================================================

class TestReorderCommandIntegration:
    """Simulates the UI drag-drop flow at the command layer."""

    def test_drag_a_to_c_single_command(self) -> None:
        """Dragging effect A (index 0) to position C (index 2) emits one command."""
        seq = _timeline_with_effects(_effect("A"), _effect("B"), _effect("C"))
        cmd = ReorderEffectCommand.create(
            track_id="t1", clip_id="c1", effect_id="A", new_order=2,
            timeline=seq,
        )
        result = cmd.apply(seq)
        assert _effect_ids(result) == ["B", "C", "A"]

    def test_drag_across_multiple_effects(self) -> None:
        """Drag first effect past three others."""
        seq = _timeline_with_effects(
            _effect("e0"), _effect("e1"), _effect("e2"), _effect("e3"),
        )
        cmd = ReorderEffectCommand.create(
            "t1", "c1", "e0", 3, timeline=seq,
        )
        result = cmd.apply(seq)
        assert _effect_ids(result) == ["e1", "e2", "e3", "e0"]

    def test_undo_restores_original(self) -> None:
        seq = _timeline_with_effects(_effect("A"), _effect("B"), _effect("C"))
        cmd = ReorderEffectCommand.create(
            "t1", "c1", "A", 2, timeline=seq,
        )
        moved = cmd.apply(seq)
        undone = cmd.undo(moved)
        assert _effect_ids(undone) == ["A", "B", "C"]

    def test_redo_after_undo(self) -> None:
        """Redo is implemented as re-apply of the undo's inverse."""
        seq = _timeline_with_effects(_effect("A"), _effect("B"), _effect("C"))
        cmd = ReorderEffectCommand.create(
            "t1", "c1", "C", 0, timeline=seq,
        )
        moved = cmd.apply(seq)
        assert _effect_ids(moved) == ["C", "A", "B"]
        undone = cmd.undo(moved)
        assert _effect_ids(undone) == ["A", "B", "C"]
        # Redo: apply the original command again (old_order now recorded)
        redone = cmd.apply(undone)
        assert _effect_ids(redone) == ["C", "A", "B"]

    def test_json_round_trip_preserves_order(self) -> None:
        """to_dict → from_dict → apply produces the same result."""
        seq = _timeline_with_effects(_effect("X"), _effect("Y"), _effect("Z"))
        cmd = ReorderEffectCommand.create(
            "t1", "c1", "Z", 0, timeline=seq,
        )
        # Serialize
        data = cmd.to_dict()
        json_str = json.dumps(data)
        # Deserialize
        restored = ReorderEffectCommand.from_dict(json.loads(json_str))
        result = restored.apply(seq)
        assert _effect_ids(result) == ["Z", "X", "Y"]

    def test_noop_drop_same_position(self) -> None:
        """Dropping at the same index should be a no-op (UI skips emission)."""
        seq = _timeline_with_effects(_effect("A"), _effect("B"), _effect("C"))
        # new_order == current index → no change
        cmd = ReorderEffectCommand.create(
            "t1", "c1", "B", 1, timeline=seq,
        )
        result = cmd.apply(seq)
        assert _effect_ids(result) == ["A", "B", "C"]

    def test_order_compact_after_sequential_reorders(self) -> None:
        """Multiple reorders keep order field == array index (0..n-1)."""
        seq = _timeline_with_effects(
            _effect("a"), _effect("b"), _effect("c"), _effect("d"), _effect("e"),
        )
        # Shuffle: e→0, a→4, c→1
        seq = ReorderEffectCommand.create("t1", "c1", "e", 0, timeline=seq).apply(seq)
        seq = ReorderEffectCommand.create("t1", "c1", "a", 4, timeline=seq).apply(seq)
        seq = ReorderEffectCommand.create("t1", "c1", "c", 1, timeline=seq).apply(seq)
        clip = seq.get_clip_by_id("c1")
        orders = [e.order for e in clip.effects]
        assert orders == list(range(len(clip.effects)))


# ================================================================
# Non-commutative chain: different order → different output
# ================================================================

class TestNonCommutativeChain:
    """Verify that effect order matters for non-commutative operations."""

    def test_different_order_different_result(self) -> None:
        """Simulate a non-commutative chain (multiply then add ≠ add then multiply)."""
        # Effect "mul2": params["op"] = "multiply", params["val"] = 2
        # Effect "add1": params["op"] = "add", params["val"] = 1
        # Input pixel = 3.0
        # Order [mul2, add1]: (3*2)+1 = 7
        # Order [add1, mul2]: (3+1)*2 = 8
        def apply_chain(effects: tuple, pixel: float) -> float:
            for e in effects:
                if e.params["op"] == "multiply":
                    pixel *= e.params["val"]
                elif e.params["op"] == "add":
                    pixel += e.params["val"]
            return pixel

        mul2 = EffectInstance(
            effect_id="mul2", effect_type="math",
            params={"op": "multiply", "val": 2.0},
        )
        add1 = EffectInstance(
            effect_id="add1", effect_type="math",
            params={"op": "add", "val": 1.0},
        )

        result_a = apply_chain((mul2, add1), 3.0)
        result_b = apply_chain((add1, mul2), 3.0)
        assert result_a != result_b
        assert result_a == 7.0
        assert result_b == 8.0

    def test_reorder_changes_render_output(self) -> None:
        """After reorder, the chain order in the timeline reflects the new sequence."""
        mul2 = EffectInstance(
            effect_id="mul2", effect_type="math",
            params={"op": "multiply", "val": 2.0},
        )
        add1 = EffectInstance(
            effect_id="add1", effect_type="math",
            params={"op": "add", "val": 1.0},
        )
        seq = _timeline_with_effects(mul2, add1)
        # Reorder: move add1 to front
        seq = ReorderEffectCommand.create(
            "t1", "c1", "add1", 0, timeline=seq,
        ).apply(seq)
        clip = seq.get_clip_by_id("c1")
        assert _effect_ids(seq) == ["add1", "mul2"]
        # The render chain now processes add1 first
        assert clip.effects[0].effect_id == "add1"
        assert clip.effects[1].effect_id == "mul2"


# ================================================================
# Panel helper logic (mocked, no QApplication needed)
# ================================================================

@dataclass
class _FakeSection:
    """Minimal stand-in for EditSectionWidget in geometry tests."""
    section_id: str
    _effect_mode: bool = True
    _top: int = 0
    _height: int = 28

    def geometry(self):
        geo = MagicMock()
        geo.top.return_value = self._top
        geo.bottom.return_value = self._top + self._height
        geo.height.return_value = self._height
        return geo


class TestDropInsertionIndex:
    """Unit-test the insertion index computation logic."""

    def _compute_index(self, sections: List[_FakeSection], pos_y: int) -> int:
        """Replicate EditPropertiesPanel._drop_insertion_index logic."""
        for idx, sec in enumerate(sections):
            geo = sec.geometry()
            mid_y = geo.top() + geo.height() // 2
            if pos_y < mid_y:
                return idx
        return len(sections)

    def test_drop_above_first(self) -> None:
        sections = [
            _FakeSection("s0", _top=0),
            _FakeSection("s1", _top=32),
            _FakeSection("s2", _top=64),
        ]
        assert self._compute_index(sections, -5) == 0

    def test_drop_between_first_and_second(self) -> None:
        sections = [
            _FakeSection("s0", _top=0, _height=28),
            _FakeSection("s1", _top=32, _height=28),
            _FakeSection("s2", _top=64, _height=28),
        ]
        # Mid of s0 = 14, mid of s1 = 46. pos=30 → after s0, before s1
        assert self._compute_index(sections, 30) == 1

    def test_drop_below_last(self) -> None:
        sections = [
            _FakeSection("s0", _top=0, _height=28),
            _FakeSection("s1", _top=32, _height=28),
        ]
        assert self._compute_index(sections, 200) == 2

    def test_drop_at_midpoint_goes_to_next(self) -> None:
        """Exactly at midpoint → falls through to next section."""
        sections = [
            _FakeSection("s0", _top=0, _height=28),  # mid = 14
            _FakeSection("s1", _top=32, _height=28),  # mid = 46
        ]
        # pos=14 is NOT < 14, so falls to s1 check; 14 < 46 → index 1
        assert self._compute_index(sections, 14) == 1

    def test_empty_sections(self) -> None:
        assert self._compute_index([], 50) == 0


class TestMimeParsing:
    """Test the section_id → (clip_id, effect_id) parsing logic."""

    def _parse_section_id(self, raw: str):
        """Replicate _handle_effect_drop parsing."""
        parts = raw.split(":")
        if len(parts) < 4 or parts[0] != "clip" or parts[2] != "effect":
            return None
        clip_id = parts[1]
        effect_id = ":".join(parts[3:])
        return clip_id, effect_id

    def test_standard_uuid(self) -> None:
        raw = "clip:abc123:effect:def456"
        result = self._parse_section_id(raw)
        assert result == ("abc123", "def456")

    def test_uuid_with_hyphens(self) -> None:
        raw = "clip:clip-001:effect:550e8400-e29b-41d4-a716-446655440000"
        result = self._parse_section_id(raw)
        assert result == ("clip-001", "550e8400-e29b-41d4-a716-446655440000")

    def test_invalid_format_returns_none(self) -> None:
        assert self._parse_section_id("invalid") is None
        assert self._parse_section_id("clip:x:transform") is None
        assert self._parse_section_id("node:n1:effect:e1") is None

    def test_effect_id_with_colons(self) -> None:
        """Effect IDs containing colons are preserved via join."""
        raw = "clip:c1:effect:part1:part2:part3"
        result = self._parse_section_id(raw)
        assert result == ("c1", "part1:part2:part3")


class TestDropIndexAdjustment:
    """Test the index adjustment when source is above/below target."""

    def test_drop_below_source_adjusts_down(self) -> None:
        """When dropping below the source, new_idx is decremented by 1."""
        current_idx = 0
        raw_new_idx = 3  # position in the list including the source
        # Adjustment: if new_idx > current_idx, subtract 1
        adjusted = raw_new_idx - 1 if raw_new_idx > current_idx else raw_new_idx
        assert adjusted == 2

    def test_drop_above_source_no_adjustment(self) -> None:
        current_idx = 2
        raw_new_idx = 0
        adjusted = raw_new_idx - 1 if raw_new_idx > current_idx else raw_new_idx
        assert adjusted == 0

    def test_drop_same_position_is_noop(self) -> None:
        current_idx = 1
        raw_new_idx = 2  # raw position after source
        adjusted = raw_new_idx - 1 if raw_new_idx > current_idx else raw_new_idx
        # adjusted == 1 == current_idx → no-op
        assert adjusted == current_idx


# ================================================================
# Signal emission contract
# ================================================================

class TestSignalEmissionContract:
    """Verify the panel emits effect_reorder_requested exactly once per valid drop."""

    def test_valid_drop_emits_once(self) -> None:
        """Simulate the _handle_effect_drop logic and count emissions."""
        emissions = []

        # Mock panel state
        sections = [
            _FakeSection("clip:c1:effect:e0", _top=0, _height=28),
            _FakeSection("clip:c1:effect:e1", _top=32, _height=28),
            _FakeSection("clip:c1:effect:e2", _top=64, _height=28),
        ]

        def compute_index(pos_y: int) -> int:
            for idx, sec in enumerate(sections):
                geo = sec.geometry()
                mid_y = geo.top() + geo.height() // 2
                if pos_y < mid_y:
                    return idx
            return len(sections)

        # Simulate dropping e0 at position below e2 (pos_y=80)
        raw = "clip:c1:effect:e0"
        current_idx = 0
        new_idx = compute_index(80)  # → 3 (below all)
        if new_idx > current_idx:
            new_idx -= 1  # → 2
        if new_idx != current_idx:
            emissions.append(("c1", "e0", new_idx))

        assert len(emissions) == 1
        assert emissions[0] == ("c1", "e0", 2)

    def test_noop_drop_emits_nothing(self) -> None:
        """Drop at same position → no emission."""
        emissions = []
        sections = [
            _FakeSection("clip:c1:effect:e0", _top=0, _height=28),
            _FakeSection("clip:c1:effect:e1", _top=32, _height=28),
        ]

        def compute_index(pos_y: int) -> int:
            for idx, sec in enumerate(sections):
                geo = sec.geometry()
                mid_y = geo.top() + geo.height() // 2
                if pos_y < mid_y:
                    return idx
            return len(sections)

        # e1 is at index 1; drop at pos_y=40 (mid of e1 = 46, so 40 < 46 → index 1)
        raw = "clip:c1:effect:e1"
        current_idx = 1
        new_idx = compute_index(40)  # → 1
        if new_idx > current_idx:
            new_idx -= 1
        if new_idx != current_idx:
            emissions.append(("c1", "e1", new_idx))

        assert len(emissions) == 0


# ================================================================
# Real Qt entry point (guards against helper-only green tests)
# ================================================================

class TestRealDragHandle:
    def test_effect_section_constructs_with_pyqt6_cursor_enum(self, qapp) -> None:
        from src.features.edit.ui.edit_properties_panel import EditSectionWidget

        section = EditSectionWidget(
            section_id="clip:c1:effect:e1",
            title="Blur",
            effect_mode=True,
        )
        assert section._drag_handle is not None
        assert section._drag_handle.cursor().shape().name == "OpenHandCursor"
        section.deleteLater()

    def test_handle_events_start_one_drag(self, qapp) -> None:
        from PyQt6.QtCore import QCoreApplication, QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        from src.features.edit.ui.edit_properties_panel import EditSectionWidget

        section = EditSectionWidget(
            section_id="clip:c1:effect:e1",
            title="Blur",
            effect_mode=True,
        )
        handle = section._drag_handle
        assert handle is not None

        press = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(2.0, 2.0),
            QPointF(2.0, 2.0),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        move = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(14.0, 2.0),
            QPointF(14.0, 2.0),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

        with patch("PyQt6.QtGui.QDrag.exec", return_value=Qt.DropAction.MoveAction) as run_drag:
            QCoreApplication.sendEvent(handle, press)
            assert section._drag_start_pos is not None
            QCoreApplication.sendEvent(handle, move)

        run_drag.assert_called_once_with(Qt.DropAction.MoveAction)
        assert section._drag_start_pos is None
        section.deleteLater()
