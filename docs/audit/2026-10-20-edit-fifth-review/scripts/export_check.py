from __future__ import annotations
import dataclasses, json, os, sys
from pathlib import Path
ROOT = Path("/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01")
OUT = Path("/tmp/topos-edit-audit-1020/out3")
sys.path.insert(0, str(ROOT))
os.environ["TOPOS_CONFIG_PATH"] = "/tmp/topos-edit-audit-1020/export-config.json"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
import src.main  # noqa
from PyQt6.QtWidgets import QApplication
from src.features.edit.core.edit_project_io import EditProjectIO
from src.features.edit.core.timeline_export import (
    TimelineExporter, TimelineExportConfig, ExportCodec, ExportRangeType,
)
from src.shared.video.frame_provider_service import VideoFrameProviderService

app = QApplication([sys.argv[0]])
io = EditProjectIO()
loaded = io.load_project(OUT / "audit-project.topos")
if loaded is None:
    raise SystemExit("load failed")
project, _, sequences = loaded
provider = VideoFrameProviderService()
results = []
try:
    for sequence in (sequences or {}).values():
        output = OUT / "export-check.mp4"
        exporter = TimelineExporter(project, sequence, provider, sequence_resolver=(sequences or {}).get)
        config = TimelineExportConfig(
            timeline_id=sequence.timeline_id, range_type=ExportRangeType.CUSTOM,
            start_frame=0, end_frame=48, codec=ExportCodec.H264,
            resolution=(640, 360), quality=20, quality_mode="crf",
            include_audio=True, output_path=str(output), burn_subtitles=False,
        )
        outcome = exporter.export_timeline(config)
        result = {"success": outcome.success, "output": str(output),
                  "frames": getattr(outcome, "frames_written", None),
                  "error": getattr(outcome, "error", None),
                  "audio_omitted": getattr(outcome, "audio_omitted", None),
                  "size": output.stat().st_size if output.exists() else 0}
        results.append(result)
        print("EXPORT " + json.dumps(result, ensure_ascii=False, default=str), flush=True)
finally:
    provider.shutdown(); io.cleanup()
(OUT / "export-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
src.main._orderly_shutdown(app, 0 if all(r["success"] for r in results) else 1)
