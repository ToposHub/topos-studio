"""Read-only probes for the second plan review; no GUI/GPU or file mutation."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def main() -> None:
    import numpy as np
    from src.shared.color.color_transform_provider import OCIOColorProvider
    from src.shared.color.color_space_descriptor import SRGB_IMAGE, LINEAR_SCENE
    from src.features.edit.core.asset_usage_index import build_asset_usage_index
    from src.features.edit.core.review_comments import ReviewComment, relocate_comment
    from src.features.edit.core.verified_ingest import ingest_files

    results: list[dict] = []
    provider = OCIOColorProvider()
    if provider.is_available():
        flat = np.full((2, 3), .5, dtype=np.float32)
        actual = provider.transform(flat.copy(), SRGB_IMAGE, LINEAR_SCENE)
        hwc = provider.transform(flat.reshape(1, 2, 3).copy(), SRGB_IMAGE, LINEAR_SCENE).reshape(2, 3)
        results.append({"id": "R01", "scope": "OCIO shape handling; no display/GPU proof",
                        "issue_present": bool(np.array_equal(actual, flat) and not np.allclose(actual, hwc)),
                        "flat_rgb": actual.tolist(), "hwc_rgb": hwc.tolist()})
    else:
        results.append({"id": "R01", "issue_present": None, "reason": "OCIO unavailable"})

    camera_clip = NS(asset_id="camera", timeline_start=0, timeline_duration=10, clip_id="c")
    sequence = NS(timeline_id="t", name="T", all_tracks=(
        NS(track_id="v1", name="V1", track_type="video", clips=(camera_clip,)),))
    project = NS(assets={"camera": NS(), "wav": NS()},
                 audio_sync_bindings={"camera": NS(wav_asset_id="wav", keep_camera_audio=False)})
    usage = build_asset_usage_index([sequence], project)
    results.append({"id": "R03", "scope": "production usage index, not full archive copy",
                    "issue_present": not usage.is_used("wav"),
                    "bound_wav_marked_unused": "wav" in usage.unused_asset_ids})

    first = NS(asset_id="camera", clip_id="first", timeline_start=0, timeline_duration=10,
               timeline_to_source_frame=lambda frame: frame)
    original = NS(asset_id="camera", clip_id="original", timeline_start=100, timeline_duration=10,
                  timeline_to_source_frame=lambda frame: frame - 100)
    comment = ReviewComment(comment_id="review", asset_id="camera", source_frame=5,
                            clip_id="original", timeline_id="t", timeline_frame=105,
                            version_hash="original-version")
    relocated = relocate_comment(comment, NS(video_tracks=(NS(clips=(first, original)),)))
    results.append({"id": "R10", "scope": "repeated source occurrence in pure model",
                    "issue_present": relocated.comment.clip_id != "original",
                    "actual_clip": relocated.comment.clip_id,
                    "actual_frame": relocated.comment.timeline_frame,
                    "reason": relocated.reason, "confidence": relocated.confidence})

    cancelled = ingest_files(["/read-only-card/a.mov"], "/never-written", cancelled_fn=lambda: True)
    results.append({"id": "R11", "scope": "report helper only; no product consumer asserted",
                    "issue_present": cancelled.all_passed,
                    "cancelled_statuses": [item.status for item in cancelled.results],
                    "all_passed": cancelled.all_passed})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
