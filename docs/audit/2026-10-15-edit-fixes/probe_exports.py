"""Independently decode the retained full-app smoke exports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "gui-session3"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import src.main  # noqa: E402,F401 -- production native-symbol isolation
import av  # noqa: E402
import numpy as np  # noqa: E402

rows: list[dict] = []
for path in sorted(OUT.glob("smoke-*.mp4")):
    with av.open(str(path)) as container:
        video = container.streams.video[0]
        audio = container.streams.audio[0] if container.streams.audio else None
        row = {"file": path.name, "width": video.width, "height": video.height,
               "fps": str(video.average_rate),
               "audio_codec": audio.codec_context.name if audio else None}
        frames = list(container.decode(video=0))
        row["video_frames"] = len(frames)
        pixels = frames[0].to_ndarray(format="rgb24")
        row["first_pixel_range"] = [int(pixels.min()), int(pixels.max())]
    with av.open(str(path)) as container:
        samples = [frame.to_ndarray().astype(np.float64)
                   for frame in container.decode(audio=0)]
        row["audio_frames"] = len(samples)
        row["audio_samples"] = sum(sample.shape[-1] for sample in samples)
        row["audio_rms"] = float(np.sqrt(
            sum(float((sample * sample).sum()) for sample in samples)
            / sum(sample.size for sample in samples)))
    assert row["video_frames"] == 49
    assert (row["width"], row["height"]) == (640, 360)
    assert row["audio_samples"] > 0 and row["audio_rms"] > 0
    rows.append(row)
assert len(rows) == 3
(OUT / "export-probe-results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
print(json.dumps(rows, indent=2))
