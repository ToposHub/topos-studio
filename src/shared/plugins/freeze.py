"""Deep freeze/thaw for plugin contract data (PR-04).

Parsed manifests, invocation params and prepared payloads must never alias
caller-owned mutable data: every nested mapping becomes a ``MappingProxyType``
and every list a tuple, recursively. ``deep_thaw`` produces detached
JSON-shaped copies (dict/list/scalars) for serialization and transport.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

DEFAULT_DEPTH_LIMIT = 64


def deep_freeze(value: Any, *, depth_limit: int = DEFAULT_DEPTH_LIMIT,
                strict_keys: bool = False, _depth: int = 0) -> Any:
    """Recursively freeze JSON data; raises ``ValueError`` on anything else."""
    if _depth > depth_limit:
        raise ValueError(f"数据嵌套超过 {depth_limit} 层")
    if isinstance(value, Mapping):
        if strict_keys and any(not isinstance(key, str) for key in value):
            raise ValueError("数据的键必须是字符串")
        return MappingProxyType({
            key: deep_freeze(item, depth_limit=depth_limit,
                             strict_keys=strict_keys, _depth=_depth + 1)
            for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item, depth_limit=depth_limit,
                                 strict_keys=strict_keys, _depth=_depth + 1)
                     for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError("数据必须由 JSON 值组成")


def deep_thaw(value: Any) -> Any:
    """Detached, JSON-shaped copy of frozen data (mappings→dict, tuples→list)."""
    if isinstance(value, Mapping):
        return {key: deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [deep_thaw(item) for item in value]
    return value


__all__ = ["deep_freeze", "deep_thaw", "DEFAULT_DEPTH_LIMIT"]
