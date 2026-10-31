"""Drop-frame timecode regression tests for EditPlaybackController.

Verifies that SMPTE 12M drop-frame timecode is produced for NTSC fractional
frame rates (29.97 / 59.94) and non-drop-frame for integer rates, and that
frame↔timecode roundtrips are exact.

The shared Timecode utility (src/shared/media/timecode.py) has its own unit
tests; these cover the controller-level wiring and the float→Rational
inference path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.edit_playback_controller import EditPlaybackController
from src.shared.media.metadata import Rational


@pytest.fixture
def controller() -> EditPlaybackController:
    return EditPlaybackController()


class TestNonDropFrameTimecode:
    """Integer fps → non-drop-frame (colon-separated)."""

    def test_24fps_ndf(self, controller: EditPlaybackController):
        controller.set_fps_rational(Rational(24, 1))
        assert controller.frame_to_timecode(0) == "00:00:00:00"
        assert controller.frame_to_timecode(24) == "00:00:01:00"
        assert controller.frame_to_timecode(24 * 60) == "00:01:00:00"
        assert controller.frame_to_timecode(24 * 3600) == "01:00:00:00"

    def test_25fps_ndf(self, controller: EditPlaybackController):
        controller.set_fps_rational(Rational(25, 1))
        assert controller.frame_to_timecode(25) == "00:00:01:00"
        assert controller.frame_to_timecode(25 * 60) == "00:01:00:00"

    def test_60fps_ndf(self, controller: EditPlaybackController):
        controller.set_fps_rational(Rational(60, 1))
        assert controller.frame_to_timecode(60) == "00:00:01:00"
        # colon, not semicolon
        assert ";" not in controller.frame_to_timecode(6000)


class TestDropFrameTimecode:
    """NTSC fractional fps → drop-frame (semicolon-separated)."""

    def test_2997_df_hour(self, controller: EditPlaybackController):
        """29.97fps: one hour = 107892 frames = 01:00:00;00 (SMPTE reference)."""
        controller.set_fps_rational(Rational(30000, 1001))
        assert controller.frame_to_timecode(0) == "00:00:00;00"
        assert controller.frame_to_timecode(107892) == "01:00:00;00"
        # The drop-frame marker ';' must be present
        assert ";" in controller.frame_to_timecode(107892)

    def test_5994_df_hour(self, controller: EditPlaybackController):
        """59.94fps: one hour = 215784 frames = 01:00:00;00."""
        controller.set_fps_rational(Rational(60000, 1001))
        assert controller.frame_to_timecode(0) == "00:00:00;00"
        assert controller.frame_to_timecode(215784) == "01:00:00;00"

    def test_2997_drops_at_first_minute(self, controller: EditPlaybackController):
        """At the first minute boundary, frames 00 and 01 are skipped (DF).

        For 29.97fps, frame 1800 maps to 00:01:00;02 (the ;00 and ;01 slots
        were dropped). NDF would label frame 1800 as 00:01:00;00.
        """
        controller.set_fps_rational(Rational(30000, 1001))
        tc = controller.frame_to_timecode(1800)
        assert tc == "00:01:00;02", tc

    def test_2997_tenth_minute_no_drop(self, controller: EditPlaybackController):
        """Every 10th minute the drop is skipped (frame 00 is valid).

        For 29.97fps, frame 17982 maps to 00:10:00;00 (the tenth-minute
        boundary is not dropped).
        """
        controller.set_fps_rational(Rational(30000, 1001))
        tc = controller.frame_to_timecode(17982)
        assert tc == "00:10:00;00", tc


class TestTimecodeRoundtrip:
    """frame → timecode → frame must be identity."""

    @pytest.mark.parametrize("fps_rat,frame", [
        (Rational(24, 1), 0),
        (Rational(24, 1), 1000),
        (Rational(25, 1), 2500),
        (Rational(30000, 1001), 0),
        (Rational(30000, 1001), 107892),
        (Rational(30000, 1001), 5234),
        (Rational(60000, 1001), 215784),
        (Rational(60000, 1001), 9999),
    ])
    def test_roundtrip(self, controller: EditPlaybackController, fps_rat: Rational, frame: int):
        controller.set_fps_rational(fps_rat)
        tc = controller.frame_to_timecode(frame)
        assert controller.timecode_to_frame(tc) == frame, (fps_rat, frame, tc)


class TestTimecodeParsing:
    """Parsing accepts NDF (:) and DF (;) as well as raw frame numbers."""

    def test_parse_ndf(self, controller: EditPlaybackController):
        controller.set_fps_rational(Rational(24, 1))
        assert controller.timecode_to_frame("00:00:01:00") == 24

    def test_parse_df_semicolon(self, controller: EditPlaybackController):
        controller.set_fps_rational(Rational(30000, 1001))
        assert controller.timecode_to_frame("01:00:00;00") == 107892

    def test_parse_raw_frame(self, controller: EditPlaybackController):
        controller.set_fps_rational(Rational(24, 1))
        assert controller.timecode_to_frame("1234") == 1234

    def test_parse_invalid_returns_none(self, controller: EditPlaybackController):
        controller.set_fps_rational(Rational(24, 1))
        assert controller.timecode_to_frame("not a tc") is None


class TestFloatFpsInference:
    """set_fps(29.97) must infer the exact NTSC Rational for DF timecode."""

    def test_2997_inferred(self, controller: EditPlaybackController):
        controller.set_fps(29.97)
        assert controller.fps_rational == Rational(30000, 1001)
        assert controller.frame_to_timecode(107892) == "01:00:00;00"

    def test_5994_inferred(self, controller: EditPlaybackController):
        controller.set_fps(59.94)
        assert controller.fps_rational == Rational(60000, 1001)

    def test_23976_inferred(self, controller: EditPlaybackController):
        controller.set_fps(23.976)
        assert controller.fps_rational == Rational(24000, 1001)

    def test_integer_fps_stays_exact(self, controller: EditPlaybackController):
        controller.set_fps(30.0)
        assert controller.fps_rational == Rational(30, 1)
        assert ";" not in controller.frame_to_timecode(900)
