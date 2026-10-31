from __future__ import annotations

import hashlib
import json

from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.render.release_fixtures import (
    build_s0_s3_fixtures,
    render_release_fixture,
)


def _scene_hash(scene: TeachingSceneDocument) -> str:
    payload = json.dumps(scene.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_s0_s3_fixtures_reopen_without_process_or_cache_state() -> None:
    for fixture_id, scene, context in build_s0_s3_fixtures():
        reopened = TeachingSceneDocument.from_json(scene.to_json())
        assert _scene_hash(reopened) == _scene_hash(scene)
        result = render_release_fixture(fixture_id, reopened, context)
        assert result.strict_consistent
