"""Independent source limits must not produce unequal linked trim deltas."""
from __future__ import annotations

import json
from src.features.edit.core.commands import CommandStack, MacroCommand
from src.features.edit.core.timeline_ops import build_numeric_trim_command
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


def main() -> None:
    video = ClipInstance("v", "asset", "v1", 100, 100, 890, 100, linked_clip_id="a")
    audio = ClipInstance("a", "asset", "a1", 100, 100, 897, 100, linked_clip_id="v")
    timeline = TimelineSequence("linked-numeric", "Linked numeric", fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(video,)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=(audio,)),))
    first, delta = build_numeric_trim_command(timeline, "v1", "v", "right", 5,
                                              mode="ripple", max_source_frames=1000)
    second, linked_delta = build_numeric_trim_command(timeline, "a1", "a", "right", delta,
        mode="ripple", max_source_frames=1000, apply_sync_lock=False)
    assert first is not None and second is not None
    changed = CommandStack().execute(MacroCommand.create("Numeric linked trim", (first, second)), timeline)
    output = {"requested_delta": 5, "video_delta": delta, "audio_delta": linked_delta,
              "video_duration": changed.get_clip_by_id("v").timeline_duration,
              "audio_duration": changed.get_clip_by_id("a").timeline_duration}
    print(json.dumps(output, indent=2))
    assert output["video_duration"] != output["audio_duration"]


if __name__ == "__main__":
    main()
