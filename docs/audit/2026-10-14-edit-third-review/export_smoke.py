"""Export a real saved audit timeline through the existing export service path."""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "evidence" / "gui-session3"
sys.path.insert(0, str(ROOT))
os.environ["TOPOS_CONFIG_PATH"] = "/tmp/topos-round3-export-config.json"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
import src.main  # noqa: E402,F401
from PyQt6.QtWidgets import QApplication  # noqa: E402
from src.features.edit.core.edit_project_io import EditProjectIO  # noqa: E402
from src.features.edit.core.timeline_export import (  # noqa: E402
    TimelineExporter, TimelineExportConfig, ExportCodec, ExportRangeType,
)
from src.shared.video.frame_provider_service import VideoFrameProviderService  # noqa: E402

app = QApplication([sys.argv[0]])
io = EditProjectIO()
loaded = io.load_project(OUT / "audit-scratch.topos")
if loaded is None:
    raise RuntimeError("Saved audit fixture could not be loaded")
project, _, sequences = loaded
provider = VideoFrameProviderService()
results: list[dict] = []
try:
    for sequence in (sequences or {}).values():
        # 49 real frames per timeline: the custom end is exclusive.
        output = OUT / ("smoke-" + sequence.timeline_id[:8] + ".mp4")
        exporter = TimelineExporter(project, sequence, provider,
                                    sequence_resolver=(sequences or {}).get)
        config = TimelineExportConfig(
            timeline_id=sequence.timeline_id, range_type=ExportRangeType.CUSTOM,
            start_frame=0, end_frame=49, codec=ExportCodec.H264,
            resolution=(640, 360), quality=20, quality_mode="crf",
            include_audio=True, output_path=str(output), burn_subtitles=False,
        )
        outcome = exporter.export_timeline(config)
        result = {"timeline": sequence.name, "output": str(output),
                  "outcome": dataclasses.asdict(outcome)}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
finally:
    provider.shutdown()
    io.cleanup()
(OUT / "export-smoke-results.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
src.main._orderly_shutdown(app, 0 if all(r["outcome"].get("success") for r in results) else 1)
