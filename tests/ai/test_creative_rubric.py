"""AQ-08: creative quality rubric for generated Motion Graphic scenes.

The rubric is a deterministic, pure-function score over a finished scene.  These
tests pin the per-dimension heuristics, the per-category weighting and the
pass/fail threshold so the "stable under a quality threshold" guarantee holds.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.ai.creative_rubric import (
    CATEGORY_WEIGHTS, DEFAULT_THRESHOLD, RubricCategory, RubricDimension,
    score_scene,
)
from src.features.teaching_animation.core import (
    create_builtin_template_registry, TemplateBuildContext,
)
from src.features.teaching_animation.core.model import (
    SCENE_SCHEMA_VERSION, SceneAssetRef, SceneElement, SceneMarker, SceneTransform,
    TeachingSceneDocument,
)


# ---------------------------------------------------------------------------
# Scene builders
# ---------------------------------------------------------------------------

def _scene(
    elements=(),
    *,
    markers=(),
    asset_refs=(),
    duration_frames: int = 120,
    background=None,
    metadata=None,
) -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="rubric_fixture", schema_version=SCENE_SCHEMA_VERSION, name="fixture",
        width=1920, height=1080, fps_num=24, fps_den=1, duration_frames=duration_frames,
        background=background or {"color": "#0b1020"},
        elements=tuple(elements), animations=(), markers=tuple(markers),
        asset_refs=tuple(asset_refs), metadata=metadata or {},
    )


def _text(element_id: str, content: str, *, font_size: float = 24.0,
          color: str = "#ffffff", z_index: int = 0) -> SceneElement:
    return SceneElement(
        element_id=element_id, type="text", z_index=z_index, visible=True,
        transform=SceneTransform(x=40.0, y=40.0, width=800.0, height=60.0),
        style={"color": color, "font_size": font_size}, content=content,
    )


# ---------------------------------------------------------------------------
# Weight sanity
# ---------------------------------------------------------------------------

class TestWeights:
    @pytest.mark.parametrize("category", list(RubricCategory))
    def test_weights_sum_to_one(self, category):
        weights = CATEGORY_WEIGHTS[category]
        assert sum(weights.values()) == pytest.approx(1.0)

    @pytest.mark.parametrize("category", list(RubricCategory))
    def test_every_dimension_weighted(self, category):
        assert set(CATEGORY_WEIGHTS[category]) == set(RubricDimension)


# ---------------------------------------------------------------------------
# Per-dimension scorers
# ---------------------------------------------------------------------------

class TestReadability:
    def test_large_nonempty_text_scores_high(self):
        scene = _scene([_text("t", "Hello", font_size=48.0)])
        s = score_scene(scene, RubricCategory.BRAND_TITLE).score_for(RubricDimension.READABILITY)
        assert s.score == pytest.approx(1.0)

    def test_empty_text_lowers_score(self):
        scene = _scene([_text("t", "", font_size=48.0), _text("t2", "ok", font_size=48.0)])
        s = score_scene(scene, RubricCategory.BRAND_TITLE).score_for(RubricDimension.READABILITY)
        assert s.score < 1.0
        assert any("empty" in n for n in s.notes)

    def test_tiny_text_flagged(self):
        scene = _scene([_text("t", "fine print", font_size=10.0)])
        s = score_scene(scene, RubricCategory.BRAND_TITLE).score_for(RubricDimension.READABILITY)
        assert s.score < 0.6
        assert any("readable size" in n for n in s.notes)

    def test_no_text_is_neutral(self):
        scene = _scene([SceneElement(
            element_id="box", type="shape", z_index=0, visible=True,
            transform=SceneTransform(x=0, y=0, width=10, height=10),
            style={"fill": "#fff"}, content={"kind": "rect"})])
        s = score_scene(scene, RubricCategory.SOCIAL_CARD).score_for(RubricDimension.READABILITY)
        assert s.score == pytest.approx(0.35)


class TestHierarchy:
    def test_three_distinct_sizes_score_full(self):
        scene = _scene([
            _text("a", "T", font_size=64.0, z_index=2),
            _text("b", "M", font_size=32.0, z_index=1),
            _text("c", "S", font_size=18.0),
        ])
        s = score_scene(scene, RubricCategory.TEACHING_EXPLANATION).score_for(RubricDimension.HIERARCHY)
        assert s.score == pytest.approx(1.0)

    def test_single_size_is_flat(self):
        scene = _scene([_text("a", "T", font_size=24.0), _text("b", "M", font_size=24.0)])
        s = score_scene(scene, RubricCategory.TEACHING_EXPLANATION).score_for(RubricDimension.HIERARCHY)
        assert s.score < 0.7


class TestBrand:
    def test_restrained_palette_with_tokens_scores_high(self):
        scene = _scene([
            _text("a", "T", font_size=24.0, color="$palette.primary"),
            SceneElement(element_id="bar", type="shape", z_index=0, visible=True,
                         transform=SceneTransform(x=0, y=0, width=10, height=10),
                         style={"fill": "$palette.primary"}, content={"kind": "rect"}),
        ])
        s = score_scene(scene, RubricCategory.BRAND_TITLE).score_for(RubricDimension.BRAND)
        assert s.score > 0.6
        assert any("token" in n for n in s.notes)

    def test_noisy_palette_penalised(self):
        elems = [_text(f"t{i}", f"c{i}", font_size=24.0, color=c) for i, c in enumerate(
            ["#111", "#222", "#333", "#444", "#555", "#666", "#777", "#888"])]
        s = score_scene(_scene(elems), RubricCategory.BRAND_TITLE).score_for(RubricDimension.BRAND)
        assert s.score < 0.6
        assert any("noisy" in n for n in s.notes)


class TestRepetition:
    def test_unique_content_scores_full(self):
        scene = _scene([_text("a", "one"), _text("b", "two"), _text("c", "three")])
        s = score_scene(scene, RubricCategory.SOCIAL_CARD).score_for(RubricDimension.REPETITION)
        assert s.score == pytest.approx(1.0)

    def test_duplicates_lower_score(self):
        scene = _scene([_text("a", "same"), _text("b", "same"), _text("c", "same")])
        s = score_scene(scene, RubricCategory.SOCIAL_CARD).score_for(RubricDimension.REPETITION)
        assert s.score < 1.0
        assert any("duplicat" in n for n in s.notes)


class TestRhythm:
    def test_static_scene_scores_low(self):
        scene = _scene([_text("a", "T")], duration_frames=180)
        s = score_scene(scene, RubricCategory.TEACHING_EXPLANATION).score_for(RubricDimension.RHYTHM)
        assert s.score < 0.4

    def test_markers_in_healthy_band_score_full(self):
        markers = tuple(SceneMarker(marker_id=f"m{i}", frame=i * 24, label=f"b{i}") for i in range(6))
        scene = _scene([_text("a", "T")], markers=markers, duration_frames=180)
        s = score_scene(scene, RubricCategory.TEACHING_EXPLANATION).score_for(RubricDimension.RHYTHM)
        assert s.score == pytest.approx(1.0)

    def test_over_dense_timing_tapers(self):
        markers = tuple(SceneMarker(marker_id=f"m{i}", frame=i, label="x") for i in range(200))
        scene = _scene([_text("a", "T")], markers=markers, duration_frames=120)
        s = score_scene(scene, RubricCategory.TEACHING_EXPLANATION).score_for(RubricDimension.RHYTHM)
        assert s.score < 1.0
        assert any("dense" in n for n in s.notes)


class TestSubtitleAudio:
    def test_audio_asset_scores_full(self):
        scene = _scene([_text("a", "T")], asset_refs=[SceneAssetRef(asset_id="a1", kind="audio")])
        s = score_scene(scene, RubricCategory.TEACHING_EXPLANATION).score_for(RubricDimension.SUBTITLE_AUDIO)
        assert s.score == pytest.approx(1.0)

    def test_markers_count_as_narration(self):
        scene = _scene([_text("a", "T")], markers=[SceneMarker(marker_id="m", frame=0, label="x")])
        s = score_scene(scene, RubricCategory.TEACHING_EXPLANATION).score_for(RubricDimension.SUBTITLE_AUDIO)
        assert s.score == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_score_is_deterministic(self):
        scene = _scene([_text("a", "T", font_size=48.0)])
        r1 = score_scene(scene, RubricCategory.BRAND_TITLE)
        r2 = score_scene(scene, RubricCategory.BRAND_TITLE)
        assert r1.to_dict() == r2.to_dict()

    def test_same_scene_weights_differently_per_category(self):
        scene = _scene([_text("a", "T", font_size=48.0)])
        teaching = score_scene(scene, RubricCategory.TEACHING_EXPLANATION).weighted_total
        social = score_scene(scene, RubricCategory.SOCIAL_CARD).weighted_total
        # Different weights -> different totals (readability differs in weight).
        assert teaching != pytest.approx(social)

    def test_pass_flag_respects_threshold(self):
        scene = _scene([_text("a", "T", font_size=48.0)])
        normal = score_scene(scene, RubricCategory.BRAND_TITLE)
        high = score_scene(scene, RubricCategory.BRAND_TITLE, threshold=0.99)
        assert normal.passed in (True, False)
        assert high.passed is False  # nothing reaches 0.99 for a bare scene

    def test_default_threshold_is_sixty(self):
        assert DEFAULT_THRESHOLD == 0.60

    def test_compiled_template_scene_passes(self):
        """A real compiled scene (good hierarchy + readability) clears the bar."""
        reg = create_builtin_template_registry()
        scene = reg.compile(reg.instantiate("title_card", {"title": "Launch", "subtitle": "v1.0"},
                                            context=TemplateBuildContext(1920, 1080, duration_frames=120)))
        r = score_scene(scene, RubricCategory.BRAND_TITLE)
        assert r.passed
