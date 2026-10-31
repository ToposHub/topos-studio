"""B4 ReferencePackage / CreativeAssetRegistry tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.creative_assets import (
    CreativeAssetRegistry,
    ReferencePackage,
)
from src.features.edit.ai_editing.generation_models import (
    GenerationModality,
    GenerationRecipe,
    ReferenceBinding,
    ReferenceKind,
)


def test_reference_change_reports_affected_recipe_and_shots() -> None:
    registry = CreativeAssetRegistry()
    registry.register_package(ReferencePackage(
        package_id="brand", bindings=(
            ReferenceBinding(ReferenceKind.PRODUCT, "product-v1"),
        ), brand_tokens={"primary": "#112233"},
    ))
    registry.register_recipe(
        GenerationRecipe(recipe_id="r1", modality=GenerationModality.VIDEO),
        package_id="brand", shot_ids=("shot-01", "shot-02"),
    )
    impact = registry.update_package(ReferencePackage(
        package_id="brand", version="2.0.0", bindings=(
            ReferenceBinding(ReferenceKind.PRODUCT, "product-v2"),
        ),
    ))
    assert impact.affected_recipe_ids == ("r1",)
    assert impact.affected_shot_ids == ("shot-01", "shot-02")
    assert registry.package_for_recipe("r1").version == "2.0.0"


def test_registry_serialization_does_not_need_paths() -> None:
    registry = CreativeAssetRegistry()
    registry.register_package(ReferencePackage(package_id="style"))
    assert "/" not in str(registry.to_dict()["packages"][0].get("path", ""))
