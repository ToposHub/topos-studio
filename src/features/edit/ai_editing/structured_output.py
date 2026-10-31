"""Versioned structured-output contracts for model-backed editing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


PLANNER_SCHEMA_VERSION = "ai-edit-planner/1.0"


class StructuredOutputError(ValueError):
    """Model output is syntactically or semantically outside the contract."""

    def __init__(self, message: str, *, code: str = "STRUCTURED_OUTPUT_INVALID") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlannerOutputEnvelope:
    schema_version: str
    variants: tuple[dict[str, Any], ...]


def parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            f"模型返回的不是合法 JSON：{text[:200]}", code="STRUCTURED_OUTPUT_JSON_INVALID"
        ) from exc
    if not isinstance(value, dict):
        raise StructuredOutputError(
            "模型返回 JSON 顶层必须是对象", code="STRUCTURED_OUTPUT_TYPE_INVALID"
        )
    return value


def validate_planner_output(
    raw: str,
    *,
    expected_schema_version: str = PLANNER_SCHEMA_VERSION,
) -> PlannerOutputEnvelope:
    """Validate the planner envelope before any domain compilation."""
    data = parse_json_object(raw)
    version = data.get("schema_version")
    if version != expected_schema_version:
        raise StructuredOutputError(
            f"planner schema_version 不匹配：期望 {expected_schema_version!r}，得到 {version!r}",
            code="PLANNER_SCHEMA_VERSION_MISMATCH",
        )
    variants = data.get("variants")
    if not isinstance(variants, list):
        raise StructuredOutputError(
            "planner variants 必须是数组", code="PLANNER_VARIANTS_INVALID"
        )
    valid: list[dict[str, Any]] = []
    for index, variant in enumerate(variants):
        if not isinstance(variant, dict):
            raise StructuredOutputError(
                f"planner variants[{index}] 必须是对象", code="PLANNER_VARIANT_INVALID"
            )
        decisions = variant.get("decisions")
        if not isinstance(decisions, list):
            raise StructuredOutputError(
                f"planner variants[{index}].decisions 必须是数组",
                code="PLANNER_DECISIONS_INVALID",
            )
        valid.append(variant)
    return PlannerOutputEnvelope(expected_schema_version, tuple(valid))


__all__ = [
    "PLANNER_SCHEMA_VERSION",
    "PlannerOutputEnvelope",
    "StructuredOutputError",
    "parse_json_object",
    "validate_planner_output",
]
