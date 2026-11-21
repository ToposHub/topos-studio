"""Translate changes between a panel snapshot and an existing audio model."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


SKIP = object()


def _identity(value: Any) -> Any:
    return value


def _always(_params: dict[str, Any]) -> bool:
    return True


@dataclass(frozen=True)
class Field:
    ui: str
    engine: str
    read: Callable[[Any], Any] = _identity
    write: Callable[[Any], Any] = _identity
    when: Callable[[dict[str, Any]], bool] = _always


def get_path(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key, SKIP)
        elif isinstance(value, (list, tuple)) and key.isdecimal() and int(key) < len(value):
            value = value[int(key)]
        else:
            return SKIP
    return value


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    target: Any = data
    for key in parts[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    key = int(parts[-1]) if isinstance(target, list) else parts[-1]
    target[key] = deepcopy(value)


def with_fallbacks(params: Any, defaults: Any) -> Any:
    """Fill missing values without dropping unknown metadata or extra list slots."""
    if isinstance(params, dict) and isinstance(defaults, dict):
        result = deepcopy(defaults)
        for key, value in params.items():
            result[key] = with_fallbacks(value, defaults.get(key))
        return result
    if isinstance(params, (list, tuple)) and isinstance(defaults, (list, tuple)):
        return [
            with_fallbacks(value, defaults[i] if i < len(defaults) else None)
            for i, value in enumerate(params)
        ] + deepcopy(list(defaults[len(params):]))
    return deepcopy(params)


def project_fields(state: dict[str, Any], params: dict[str, Any], fields: tuple[Field, ...]) -> None:
    for field in fields:
        value = get_path(params, field.engine)
        if value is not SKIP and field.when(params):
            projected = field.read(value)
            if projected is not SKIP:
                set_path(state, field.ui, projected)


def collect_fields(
    state: dict[str, Any], baseline: dict[str, Any], params: dict[str, Any], fields: tuple[Field, ...],
) -> set[str]:
    """Apply only controls the user changed, including when a display rounds an old value."""
    changed: set[str] = set()
    for field in fields:
        value = get_path(state, field.ui)
        if value is SKIP or value == get_path(baseline, field.ui) or not field.when(params):
            continue
        stored = field.write(value)
        if stored is not SKIP:
            set_path(params, field.engine, stored)
            changed.add(field.engine.split(".", 1)[0])
    return changed
