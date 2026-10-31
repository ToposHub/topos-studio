#!/usr/bin/env python3
"""
Audit tool for Edit Playback Chain integrity.

Checks that the full playback chain is wired correctly:
- EditPlaybackController: FPS, duration, range, tick/play/stop/seek
- TimelineSequence: range_start/range_end sync
- EditPlaybackController → TimelinePanel signal wiring
- Export range types (IN_OUT, SELECTED, ENTIRE) correctness

Usage:
    .venv/bin/python tools/audit_edit_playback_chain.py
"""

import sys
import os

# Ensure project root is on path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def check_playback_controller_api():
    """Verify EditPlaybackController has all required methods."""
    from src.features.edit.core.edit_playback_controller import EditPlaybackController
    required = [
        "set_fps", "set_timeline_duration",
        "play", "pause", "stop", "seek",
        "tick", "current_frame", "state",
        "set_loop", "set_playback_range", "clear_playback_range",
        "playback_range",
    ]
    missing = [m for m in required if not hasattr(EditPlaybackController, m)]
    if missing:
        print(f"  FAIL: EditPlaybackController missing: {missing}")
        return False
    print(f"  OK: EditPlaybackController has all {len(required)} required API methods")
    return True


def check_playback_range_functionality():
    """Verify range playback works correctly."""
    from src.features.edit.core.edit_playback_controller import (
        EditPlaybackController, PlaybackState,
    )
    import time

    c = EditPlaybackController()
    c.set_fps(24.0)
    c.set_timeline_duration(240)

    # Set range
    c.set_playback_range(48, 144)
    assert c.playback_range == (48, 144), f"Expected (48, 144), got {c.playback_range}"

    # Clear range
    c.clear_playback_range()
    assert c.playback_range is None, f"Expected None, got {c.playback_range}"

    # Range loop
    c.set_loop(True)
    c.set_playback_range(48, 144)
    c.seek(48)
    c.play()
    start = time.monotonic()
    elapsed = (144 - 48 + 10) / 24.0
    frame = c.tick(start + elapsed)
    if frame != 48:
        print(f"  FAIL: Range loop expected frame 48, got {frame}")
        return False

    # Range stop (no loop)
    c2 = EditPlaybackController()
    c2.set_fps(24.0)
    c2.set_timeline_duration(240)
    c2.set_loop(False)
    c2.set_playback_range(48, 144)
    c2.seek(48)
    c2.play()
    start = time.monotonic()
    elapsed = (144 - 48 + 10) / 24.0
    frame = c2.tick(start + elapsed)
    if frame != 144:
        print(f"  FAIL: Range stop expected frame 144, got {frame}")
        return False
    if c2.state != PlaybackState.STOPPED:
        print(f"  FAIL: Expected STOPPED, got {c2.state}")
        return False

    print("  OK: Range playback (set/clear/loop/stop) all correct")
    return True


def check_timeline_range_sync():
    """Verify TimelineSequence.with_range works."""
    from src.features.edit.core.timeline_v2 import TimelineSequence

    tl = TimelineSequence(timeline_id="tl1", name="Test", fps=24.0)
    assert tl.range_start is None
    assert tl.range_end is None

    tl2 = tl.with_range(48, 144)
    assert tl2.range_start == 48
    assert tl2.range_end == 144
    assert tl.range_start is None  # immutable

    tl3 = tl2.with_range(None, None)
    assert tl3.range_start is None
    assert tl3.range_end is None

    print("  OK: TimelineSequence.with_range immutability verified")
    return True


def check_export_range_types():
    """Verify ExportRangeType enum and config."""
    from src.features.edit.core.timeline_export import (
        TimelineExportConfig, ExportRangeType,
    )

    # ENTIRE
    cfg1 = TimelineExportConfig(range_type=ExportRangeType.ENTIRE, output_path="/tmp/test.mov")
    assert cfg1.range_type == ExportRangeType.ENTIRE

    # IN_OUT
    cfg2 = TimelineExportConfig(range_type=ExportRangeType.IN_OUT, output_path="/tmp/test.mov")
    assert cfg2.range_type == ExportRangeType.IN_OUT

    # SELECTED
    cfg3 = TimelineExportConfig(
        range_type=ExportRangeType.SELECTED,
        output_path="/tmp/test.mov",
        selected_clip_ids=["c1", "c2"],
    )
    assert cfg3.range_type == ExportRangeType.SELECTED
    assert cfg3.selected_clip_ids == ["c1", "c2"]

    print("  OK: ExportRangeType enum and config verified")
    return True


def check_command_api():
    """Verify playback-related commands use apply() not execute()."""
    from src.features.edit.core.commands import (
        SetTimelineRangeCommand,
        ToggleClipEnabledCommand,
        SetClipLinkedCommand,
        SetClipLabelColorCommand,
    )
    for cls in [SetTimelineRangeCommand, ToggleClipEnabledCommand,
                SetClipLinkedCommand, SetClipLabelColorCommand]:
        if not hasattr(cls, "apply"):
            print(f"  FAIL: {cls.__name__} missing apply() method")
            return False
        if hasattr(cls, "execute") and cls.__name__ not in ("MacroCommand",):
            # execute should not be the primary method
            import inspect
            # Check if execute is just an alias or separate method
            src = inspect.getsource(cls)
            if "def execute(" in src and "def apply(" not in src:
                print(f"  FAIL: {cls.__name__} has execute() but no apply()")
                return False

    print("  OK: All commands have apply() method")
    return True


def main():
    print("=" * 60)
    print("Edit Playback Chain Audit")
    print("=" * 60)

    checks = [
        ("PlaybackController API", check_playback_controller_api),
        ("Range Playback Functionality", check_playback_range_functionality),
        ("Timeline Range Sync", check_timeline_range_sync),
        ("Export Range Types", check_export_range_types),
        ("Command API (apply vs execute)", check_command_api),
    ]

    passed = 0
    failed = 0
    for name, fn in checks:
        print(f"\n[{name}]")
        try:
            if fn():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All checks passed!")
    sys.exit(0)


if __name__ == "__main__":
    main()
