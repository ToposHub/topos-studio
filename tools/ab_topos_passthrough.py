#!/usr/bin/env python3
"""A/B harness: Topos-source passthrough vs compositor baseline outputs."""

from __future__ import annotations

import sys
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_ROOT))

from tools.repro_first_frame_black import build_case, VIDEO_DIR  # noqa: E402

from src.features.edit.core.timeline_export import (  # noqa: E402
    ExportCodec,
    ExportRangeType,
    ExportSource,
    TimelineExportConfig,
    create_timeline_exporter,
)

MATRIX = [
    ("h264", ExportCodec.H264),
    ("prores_422", ExportCodec.PRORES_422),
    ("dnxhr_lb", ExportCodec.DNXHR_LB),
    ("topos_proxy", ExportCodec.TOPOS_PROXY),
]


def main() -> int:
    out_dir = Path(sys.argv[1])
    if not out_dir.is_absolute():
        out_dir = TEST_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = int(sys.argv[2]) if len(sys.argv) > 2 else 48

    source = VIDEO_DIR / "2K_topos_422_HQ.mov"
    project, sequence = build_case(source, frames)
    for name, codec in MATRIX:
        output = out_dir / f"{name}.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=codec,
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project, sequence=sequence)
        outcome = exporter.export_timeline(config)
        status = "ok" if outcome.success else f"FAILED {outcome.cause}"
        print(f"{name}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
