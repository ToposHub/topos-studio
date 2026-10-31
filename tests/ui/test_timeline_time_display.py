"""T25 view tests: TimelineTimeDisplay current-frame readout + jump box.

Locks the AE-style two-line time display (plan §16.3 B1):
  * top line — large blue timecode / seconds / frame (follows display mode)
  * bottom line — small blue "frame (fps)"
  * click-to-edit, Enter to commit, Esc to cancel
  * invalid input flashes an error border and does NOT move the playhead
  * fixed widget width so digit-count changes don't shift neighbours

The widget is GUI-bearing, so these tests render under offscreen and assert
on the widget's state + emitted signals rather than pixel content.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.ui.timeline_time_display import (
    TimelineTimeDisplay, _FIXED_WIDTH_PX,
)


pytestmark = pytest.mark.composite_core


# ── construction + defaults ───────────────────────────────────────────


class TestDefaults:
    def test_fixed_width_set(self, qapp):
        """Plan §16.3 B1: width fixed at 104-112 px so the layout doesn't
        jump when the frame number changes digit count."""
        w = TimelineTimeDisplay()
        assert w.width() == _FIXED_WIDTH_PX
        assert 104 <= _FIXED_WIDTH_PX <= 112

    def test_default_fps_24(self, qapp):
        w = TimelineTimeDisplay()
        assert w._fps == 24.0

    def test_default_frame_zero(self, qapp):
        w = TimelineTimeDisplay()
        assert w.current_frame == 0

    def test_default_display_mode_frame(self, qapp):
        w = TimelineTimeDisplay()
        assert w._display_mode == "frame"


# ── frame → label formatting ──────────────────────────────────────────


class TestFormatting:
    def test_frame_mode_top_zero_padded(self, qapp):
        w = TimelineTimeDisplay(fps=24.0)
        w.set_display_mode("frame")
        w.set_current_frame(49)
        assert w._top_label.text() == "00049"

    def test_timecode_mode_basic(self, qapp):
        w = TimelineTimeDisplay(fps=24.0)
        w.set_display_mode("timecode")
        w.set_current_frame(0)
        assert w._top_label.text() == "0:00:00:00"
        w.set_current_frame(24)
        assert w._top_label.text() == "0:00:01:00"
        w.set_current_frame(24 * 60 + 12)
        assert w._top_label.text() == "0:01:00:12"

    def test_seconds_mode(self, qapp):
        w = TimelineTimeDisplay(fps=24.0)
        w.set_display_mode("seconds")
        w.set_current_frame(48)
        # 48 / 24 = 2.000s
        assert w._top_label.text().strip() == "2.000s"

    def test_bottom_line_always_frame_plus_fps(self, qapp):
        """The bottom line is always "frame (fps)" regardless of mode
        (plan §16.3 B1)."""
        w = TimelineTimeDisplay(fps=29.97)
        w.set_current_frame(49)
        for mode in ("frame", "seconds", "timecode"):
            w.set_display_mode(mode)
            assert w._bottom_label.text() == "49 (29.97)"

    def test_set_fps_updates_bottom_line(self, qapp):
        w = TimelineTimeDisplay(fps=24.0)
        w.set_current_frame(10)
        assert "(24)" in w._bottom_label.text()
        w.set_fps(30.0)
        assert "(30)" in w._bottom_label.text()

    def test_unknown_display_mode_falls_back_to_frame(self, qapp):
        w = TimelineTimeDisplay()
        w.set_display_mode("nonsense")
        assert w._display_mode == "frame"


# ── input parsing ─────────────────────────────────────────────────────


class TestInputParsing:
    @pytest.fixture
    def display(self, qapp):
        return TimelineTimeDisplay(fps=24.0)

    def test_pure_integer_parsed_as_frame(self, display):
        assert display._parse_user_input("49") == 49

    def test_negative_frame_parsed(self, display):
        """Negative frames are valid (Fusion-style composition origin)."""
        assert display._parse_user_input("-5") == -5

    def test_timecode_full_form(self, display):
        assert display._parse_user_input("0:00:02:01") == 49  # 2s + 1f
        assert display._parse_user_input("1:00:00:00") == 86400  # 1h

    def test_timecode_partial_right_aligned(self, display):
        """Partial timecode is right-aligned: the trailing fields map to the
        lowest-order slots (S, F), and the leading slots (H, M) are filled
        with zero. So "2:01" → 0:0:2:1 = 2 seconds + 1 frame = 49 frames.

        This matches AE's goto-time convention where you type the least-
        significant fields and the higher-order ones default to zero.
        """
        assert display._parse_user_input("2:01") == 49
        # "1:2:3" → 0:1:2:3 = 1 min + 2 s + 3 f
        assert display._parse_user_input("1:2:3") == (62 * 24 + 3)

    def test_seconds_with_s_suffix(self, display):
        # 2.042s → round(2.042 * 24) = 49
        assert display._parse_user_input("2.042s") == 49

    def test_seconds_bare_decimal_in_seconds_mode(self, display):
        display.set_display_mode("seconds")
        # In seconds mode a bare "2.0" is interpreted as seconds.
        assert display._parse_user_input("2.0") == 48

    def test_empty_input_returns_none(self, display):
        assert display._parse_user_input("") is None
        assert display._parse_user_input("   ") is None

    def test_garbage_returns_none(self, display):
        assert display._parse_user_input("abc") is None
        assert display._parse_user_input("1:2:3:4:5") is None

    def test_timecode_invalid_seconds_rejected(self, display):
        assert display._parse_user_input("0:00:99:00") is None

    def test_timecode_invalid_frame_for_fps_rejected(self, display):
        # fps=24, frame field 30 is out of range.
        assert display._parse_user_input("0:00:00:30") is None


# ── edit / commit / cancel ────────────────────────────────────────────


class TestEditLifecycle:
    @pytest.fixture
    def display(self, qapp):
        w = TimelineTimeDisplay(fps=24.0)
        w.set_current_frame(10)
        return w

    def test_not_editing_by_default(self, display):
        assert display._editing is False
        assert display._edit.isHidden()

    def test_click_enters_edit_mode(self, display):
        display._enter_edit()
        assert display._editing is True
        assert not display._edit.isHidden()
        # Edit field pre-filled with current top-line text.
        assert display._edit.text() == display._format_top(10)

    def test_commit_valid_frame_emits_signal(self, display):
        received = []
        display.frame_committed.connect(received.append)
        display._enter_edit()
        display._edit.setText("49")
        display._on_edit_commit()
        assert received == [49]
        assert display._editing is False
        assert display.current_frame == 49

    def test_commit_invalid_does_not_emit(self, display):
        received = []
        display.frame_committed.connect(received.append)
        display._enter_edit()
        display._edit.setText("garbage")
        display._on_edit_commit()
        assert received == []
        # Stays in edit mode so the user can fix the input.
        assert display._editing is True
        # Current frame unchanged.
        assert display.current_frame == 10

    def test_cancel_exits_without_committing(self, display):
        received = []
        display.frame_committed.connect(received.append)
        display._enter_edit()
        display._edit.setText("49")
        display._on_edit_cancel()
        assert received == []
        assert display._editing is False
        # Frame unchanged.
        assert display.current_frame == 10

    def test_commit_same_frame_does_not_emit(self, display):
        """Re-committing the current frame is a no-op (no spurious signal)."""
        received = []
        display.frame_committed.connect(received.append)
        display._enter_edit()
        display._edit.setText("10")  # same as current
        display._on_edit_commit()
        assert received == []


# ── paint robustness ──────────────────────────────────────────────────


class TestPaintRobustness:
    @pytest.mark.parametrize("mode", ["frame", "seconds", "timecode"])
    @pytest.mark.parametrize("frame", [0, 1, 24, 100, 1000, -5])
    def test_label_update_does_not_throw(self, qapp, mode, frame):
        w = TimelineTimeDisplay(fps=24.0)
        w.set_display_mode(mode)
        w.set_current_frame(frame)
        w.repaint()
