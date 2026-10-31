#!/usr/bin/env python3
"""Run the deterministic S0-S3 SVG release fixture gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.features.teaching_animation.render.release_fixtures import (
    build_s0_s3_fixtures,
    render_release_fixture,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    payload = {"schema_version": 1, "fixtures": []}
    passed = True
    for fixture_id, scene, context in build_s0_s3_fixtures():
        result = render_release_fixture(fixture_id, scene, context)
        fixture = result.manifest.to_dict()
        fixture.update({
            "fixture_id": fixture_id,
            "frames": list(result.frames),
            "strict_consistent": result.strict_consistent,
            "frame_hashes": [item["frame_hash"] for item in result.manifest.frame_evidence],
        })
        payload["fixtures"].append(fixture)
        passed = passed and result.strict_consistent
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
