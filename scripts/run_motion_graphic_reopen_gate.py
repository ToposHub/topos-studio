#!/usr/bin/env python3
"""Validate that release fixtures survive JSON save/reopen in a clean process."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.render.release_fixtures import (
    build_s0_s3_fixtures,
    render_release_fixture,
)


def _scene_hash(scene: TeachingSceneDocument) -> str:
    payload = json.dumps(scene.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    fixtures: list[dict[str, object]] = []
    passed = True
    for fixture_id, scene, context in build_s0_s3_fixtures():
        reopened = TeachingSceneDocument.from_json(scene.to_json())
        result = render_release_fixture(fixture_id, reopened, context)
        record = {
            "fixture_id": fixture_id,
            "scene_id": reopened.scene_id,
            "scene_hash_before": _scene_hash(scene),
            "scene_hash_after": _scene_hash(reopened),
            "strict_consistent": result.strict_consistent,
            "declared_asset_ids": list(result.manifest.declared_asset_ids),
        }
        fixtures.append(record)
        passed = passed and _scene_hash(scene) == _scene_hash(reopened) and result.strict_consistent
    encoded = json.dumps({"schema_version": 1, "fixtures": fixtures}, indent=2) + "\n"
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
