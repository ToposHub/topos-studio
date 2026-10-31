"""CreativeAssetRegistry / ReferencePackage（M11 B4）。

该注册表只保存 asset id、reference 角色、品牌 token 和 recipe 影响关系，不保存媒体
绝对路径或 prompt 原文。它是生成前的可追溯上下文；实际 MediaAsset 导入仍由既有
MediaPoolImporter 接线，timeline 仍只能通过 EditPlan 写入。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.features.edit.ai_editing.generation_models import (
    GenerationRecipe,
    ReferenceBinding,
)


@dataclass(frozen=True)
class ReferencePackage:
    package_id: str
    version: str = "1.0.0"
    label: str = ""
    bindings: tuple[ReferenceBinding, ...] = ()
    brand_tokens: Mapping[str, str] = field(default_factory=dict)
    provenance_ref: str = ""

    def __post_init__(self) -> None:
        if not self.package_id:
            raise ValueError("reference package_id cannot be empty")

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(binding.asset_id for binding in self.bindings))

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self._payload(), sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _payload(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "version": self.version,
            "label": self.label,
            "bindings": [binding.to_dict() for binding in self.bindings],
            "brand_tokens": dict(self.brand_tokens),
            "provenance_ref": self.provenance_ref,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["asset_ids"] = list(self.asset_ids)
        payload["fingerprint"] = self.fingerprint if self.package_id else ""
        return payload


@dataclass(frozen=True)
class RecipeImpact:
    package_id: str
    affected_recipe_ids: tuple[str, ...] = ()
    affected_shot_ids: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "affected_recipe_ids": list(self.affected_recipe_ids),
            "affected_shot_ids": list(self.affected_shot_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class _RecipeRecord:
    recipe: GenerationRecipe
    package_id: str
    shot_ids: tuple[str, ...] = ()


class CreativeAssetRegistry:
    """会话级 reference/recipe 关系表；变更返回显式影响报告。"""

    def __init__(self) -> None:
        self._packages: dict[str, ReferencePackage] = {}
        self._recipes: dict[str, _RecipeRecord] = {}

    def register_package(self, package: ReferencePackage) -> None:
        """注册一个**新的** reference package；重复 id 显式拒绝（AUD-13300）。

        变更已注册的 package 必须走 :meth:`update_package`——它返回的
        ``RecipeImpact`` 是类契约（"变更返回显式影响报告"）的一部分；
        ``register_package`` 静默覆盖会让引用该 package 的 recipe 凭空
        变 stale 且不产生任何影响报告，绕过复核链路。
        """
        if package.package_id in self._packages:
            raise ValueError(
                f"reference package already registered: {package.package_id!r}; "
                "use update_package() to change an existing package"
            )
        self._packages[package.package_id] = package

    def get_package(self, package_id: str) -> ReferencePackage | None:
        return self._packages.get(package_id)

    def list_packages(self) -> tuple[ReferencePackage, ...]:
        return tuple(self._packages.values())

    def register_recipe(
        self,
        recipe: GenerationRecipe,
        *,
        package_id: str = "",
        shot_ids: tuple[str, ...] = (),
    ) -> None:
        if package_id and package_id not in self._packages:
            raise KeyError(f"reference package not registered: {package_id}")
        self._recipes[recipe.recipe_id] = _RecipeRecord(
            recipe=recipe, package_id=package_id, shot_ids=tuple(dict.fromkeys(shot_ids)),
        )

    def impacted_by_package(self, package_id: str) -> RecipeImpact:
        records = [record for record in self._recipes.values() if record.package_id == package_id]
        return RecipeImpact(
            package_id=package_id,
            affected_recipe_ids=tuple(record.recipe.recipe_id for record in records),
            affected_shot_ids=tuple(dict.fromkeys(
                shot_id for record in records for shot_id in record.shot_ids
            )),
            reason="reference package changed; regenerate or revalidate affected recipes",
        )

    def update_package(self, package: ReferencePackage) -> RecipeImpact:
        """更新 reference 后先保存新版本，再返回待复核的 recipe/shot 清单。"""
        existed = package.package_id in self._packages
        self._packages[package.package_id] = package
        impact = self.impacted_by_package(package.package_id)
        if not existed:
            return RecipeImpact(package_id=package.package_id, reason="reference package registered")
        return impact

    def package_for_recipe(self, recipe_id: str) -> ReferencePackage | None:
        record = self._recipes.get(recipe_id)
        return self._packages.get(record.package_id) if record and record.package_id else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "packages": [package.to_dict() for package in self.list_packages()],
            "recipes": [
                {
                    "recipe_id": record.recipe.recipe_id,
                    "package_id": record.package_id,
                    "shot_ids": list(record.shot_ids),
                    "recipe": record.recipe.to_dict(),
                }
                for record in self._recipes.values()
            ],
        }


__all__ = ["ReferencePackage", "RecipeImpact", "CreativeAssetRegistry"]
