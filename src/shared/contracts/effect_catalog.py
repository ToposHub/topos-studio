"""DESK-06B · 效果/转场目录共享契约（双端同一份机器可读目录）。

问题
----
移动端与桌面端要"同切点/同时长/同参数"地应用效果。此前效果语义只存在
桌面代码里（EffectRegistry / transition_catalog），移动端只能"以中文名
猜效果"——两边各写一套参数名/值域，往返即漂移。

契约
----
- ``data/effect_catalog_v1.json``：由桌面注册表**生成**的机器可读目录
  （效果 effect_type/name/category/params 值域默认值 + 转场
  transition_type/label/category/spatial）。移动端从这份文件生成自己的
  效果面板与参数模型，不做手抄。
- 演进规则（冻结）：**只增不改**——新增条目直接追加；参数语义/值域
  变化必须换新 ``effect_type``（旧类型保留别名或移除需 bump 目录
  set id 并双端同步）。目录摘要（SHA-256）供双端核对同版本。
- effect_type / transition_type 是跨端稳定标识：包里引用的就是这些 id；
  未识别 id 在桌面侧由 DESK-04B 分级锁定（E/P/B/X），不猜。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = [
    "CATALOG_SET_ID",
    "CATALOG_FILENAME",
    "build_catalog",
    "catalog_path",
    "write_catalog_file",
    "load_catalog_file",
    "catalog_digest",
]

CATALOG_SET_ID = "topos.effect-catalog.v1"
CATALOG_FILENAME = "effect_catalog_v1.json"
_DATA_DIR = Path(__file__).parent / "data"


def _param_schema_dict(schema: Any) -> dict[str, Any]:
    return {
        "type": getattr(schema, "type", "float"),
        "default": getattr(schema, "default", None),
        "min": getattr(schema, "min_value", None),
        "max": getattr(schema, "max_value", None),
        "label": getattr(schema, "label", ""),
    }


def build_catalog() -> dict[str, Any]:
    """从生产注册表生成目录（确定性：按 type 排序）。"""
    from src.features.edit.core.effect_registry import get_effect_registry
    from src.features.edit.core.transition_catalog import (
        TRANSITION_DEFINITIONS,
    )

    effects = []
    for definition in sorted(
            get_effect_registry().get_all_definitions(),
            key=lambda d: d.effect_type):
        if definition.effect_type.startswith("plugin:"):
            # 动态注册的用户插件：内容随安装环境变化，不属于跨端冻结
            # 目录（移动端遇到 plugin:* 类型按 04B 未识别分级处理）。
            continue
        effects.append({
            "effect_type": definition.effect_type,
            "name": definition.name,
            "category": definition.category,
            "description": definition.description,
            "params": {
                name: _param_schema_dict(schema)
                for name, schema in sorted(
                    definition.params_schema.items())
            },
        })

    transitions = []
    for definition in sorted(TRANSITION_DEFINITIONS,
                             key=lambda d: d.transition_type):
        if getattr(definition, "source", "builtin") != "builtin":
            continue  # 用户自定义转场不属于跨端冻结目录
        transitions.append({
            "transition_type": definition.transition_type,
            "label": definition.label,
            "category": definition.category,
            "spatial": bool(definition.spatial),
        })

    return {
        "catalog_id": CATALOG_SET_ID,
        "evolution": "append-only; semantic param changes require a new type id",
        "effects": effects,
        "transitions": transitions,
    }


def catalog_path() -> Path:
    return _DATA_DIR / CATALOG_FILENAME


def write_catalog_file(path: Path | None = None) -> Path:
    target = path or catalog_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(build_catalog(), indent=2, sort_keys=True,
                   ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8")
    return target


def load_catalog_file(path: Path | None = None) -> dict[str, Any]:
    return json.loads(
        (path or catalog_path()).read_text(encoding="utf-8"))


def catalog_digest(catalog: dict[str, Any] | None = None) -> str:
    data = catalog if catalog is not None else build_catalog()
    canonical = json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
