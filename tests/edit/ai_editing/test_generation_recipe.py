"""B4 GenerationRecipe / ReferenceBinding contract tests."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.generation_models import (
    GenerationModality,
    GenerationRecipe,
    GenerationRequest,
    GenerationConsent,
    ReferenceBinding,
    ReferenceKind,
)


def _consent() -> GenerationConsent:
    return GenerationConsent(
        confirmed=True, provider_id="seedance",
        cost_acknowledged=True, upload_acknowledged=True,
        copyright_acknowledged=True,
    )


def test_recipe_round_trip_and_reference_mapping() -> None:
    recipe = GenerationRecipe(
        recipe_id="recipe-1", modality=GenerationModality.VIDEO,
        width=1280, height=720, fps=24.0, duration_seconds=4.0,
        references=(
            ReferenceBinding(ReferenceKind.FIRST_FRAME, "asset-start"),
            ReferenceBinding(ReferenceKind.PRODUCT, "product-1", weight=0.8),
        ),
    )
    request = GenerationRequest(
        request_id="request-1", modality=GenerationModality.VIDEO,
        prompt="产品镜头", consent=_consent(), recipe=recipe,
    )
    assert request.effective_reference_asset_ids == ("asset-start", "product-1")
    data = request.to_dict()
    json.dumps(data)
    assert data["recipe"]["schema_version"] == "1.0"
    assert data["reference_bindings"][0]["kind"] == "first_frame"


def test_recipe_migrates_legacy_resolution() -> None:
    recipe = GenerationRecipe.migrate({
        "id": "old", "modality": "video", "resolution": "1920x1080",
        "fps": 30, "references": [{"kind": "style", "asset_id": "style-1"}],
    })
    assert recipe.schema_version == "1.0"
    assert (recipe.width, recipe.height, recipe.fps) == (1920, 1080, 30.0)
    assert recipe.reference_asset_ids == ("style-1",)


def test_recipe_rejects_mismatched_request_modality() -> None:
    recipe = GenerationRecipe(recipe_id="image", modality=GenerationModality.IMAGE)
    with pytest.raises(ValueError):
        GenerationRequest(
            request_id="r", modality=GenerationModality.VIDEO,
            prompt="x", consent=_consent(), recipe=recipe,
        )
