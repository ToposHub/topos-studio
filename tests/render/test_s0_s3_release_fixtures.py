from __future__ import annotations

import hashlib

from src.features.teaching_animation.render.release_fixtures import (
    build_s0_s3_fixtures,
    render_release_fixture,
)


def test_s0_s3_fixture_manifest_is_strictly_repeatable() -> None:
    results = [
        render_release_fixture(fixture_id, scene, context)
        for fixture_id, scene, context in build_s0_s3_fixtures()
    ]
    assert [item.fixture_id for item in results] == ["S0", "S1", "S2", "S3"]
    assert all(item.strict_consistent for item in results)
    assert all(item.manifest.manifest_hash for item in results)

    s1 = results[1]
    assert "教学动画" in s1.first[0].data.decode("utf-8")

    s2 = results[2]
    assert s2.manifest.alpha_mode == "premult"
    assert '<rect width="100%" height="100%"' not in s2.first[0].data.decode("utf-8")

    s3 = results[3]
    rendered = s3.first[0].data.decode("utf-8")
    assert '#7f1d1d' in rendered
    assert s3.manifest.declared_asset_ids == ("missing-image",)
    assert "glow:dropped" in s3.manifest.capability_fallback


def test_s0_s3_frame_hashes_are_fixed_golden_values() -> None:
    # The SVG renderer is byte-oriented here; these hashes catch accidental
    # changes to locale, alpha, missing-asset placeholder, or fallback output.
    expected = {
        "S0": (
            "1b3e258288df1c39d883890963fbc60e53ea7a695a8a59f869768b7c0149cc04",
        ) * 3,
        "S1": (
            "4c829da45063459196669332c5d933e134919ac4ee8a1a5dfd134e6e941a2e7b",
        ) * 3,
        "S2": (
            "aad34ce8bcae1f60a4997958f02726757e7610b396506d312238145f0d0dd108",
        ) * 3,
        "S3": (
            "f5299e0e1d9cd0130bca0dd0faa8fe7da0adedc6c35d00ae9f9f09272692b6a5",
        ) * 3,
    }
    for fixture_id, scene, context in build_s0_s3_fixtures():
        result = render_release_fixture(fixture_id, scene, context)
        hashes = tuple(
            hashlib.sha256(frame.data).hexdigest() for frame in result.first
        )
        assert hashes == expected[fixture_id]
