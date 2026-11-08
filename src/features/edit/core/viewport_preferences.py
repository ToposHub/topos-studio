"""VP-B09：Viewport 用户偏好——版本化、校验、旧键迁移（纯领域）。

缺口（计划 VP-B09）：单/双模式与 splitter 尺寸已有本地配置 key
（``edit_viewport_dual_view``／``edit_viewport_splitter_sizes``，前端接线），
但没有版本化的 viewport 配置段：类型/枚举校验、未知值回落（单视图
Timeline）与配置诊断、旧扁平 key 的迁移、重启活动对象偏好都没有统一
入口。

契约（``ViewportPreferences``，纯领域，宿主负责读写盘与 debounce）：

- ``parse`` 接受新版 versioned dict 或旧扁平键组合（迁移）；任何类型/
  枚举/范围错误 → 该项回落默认并**记录诊断**（不抛错、不清空其余项）；
- 未知版本 → 整体回落默认（单视图 Timeline）+ 诊断；
- ``single_view_target`` ∈ {"source","timeline"}（重启默认活动对象）；
- splitter 尺寸：恰好 2 个、非 bool 正 int、上限 100000；
- 实际播放中状态、texture、绝对路径、解码缓存一律不入配置。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

VIEWPORT_PREFS_VERSION = 1

SINGLE_VIEW_SOURCE = "source"
SINGLE_VIEW_TIMELINE = "timeline"
_VALID_TARGETS = (SINGLE_VIEW_SOURCE, SINGLE_VIEW_TIMELINE)

_SPLITTER_MAX = 100000

LEGACY_KEY_DUAL_VIEW = "edit_viewport_dual_view"
LEGACY_KEY_SPLITTER_SIZES = "edit_viewport_splitter_sizes"


@dataclass(frozen=True)
class ViewportPreferences:
    """版本化的 Viewport 显示偏好（只含可序列化的显示状态）。"""

    version: int = VIEWPORT_PREFS_VERSION
    dual_view: bool = False
    splitter_sizes: Optional[Tuple[int, int]] = None
    single_view_target: str = SINGLE_VIEW_TIMELINE

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "dual_view": self.dual_view,
            "splitter_sizes": (list(self.splitter_sizes)
                               if self.splitter_sizes is not None else None),
            "single_view_target": self.single_view_target,
        }


def _valid_bool(value: Any, field: str, prefs: dict[str, Any],
                diagnostics: List[str], default: bool) -> None:
    if isinstance(value, bool):
        prefs[field] = value
    else:
        prefs[field] = default
        diagnostics.append(
            f"{field}: expected bool, got {type(value).__name__}; "
            f"reset to {default}")


def _valid_splitter(value: Any, prefs: dict[str, Any],
                    diagnostics: List[str]) -> None:
    if value is None:  # 缺失 = 静默默认（只有显式坏值才诊断）
        prefs["splitter_sizes"] = None
        return
    if isinstance(value, list) and len(value) == 2:
        sizes = []
        ok = True
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int) \
                    or item <= 0 or item > _SPLITTER_MAX:
                ok = False
                break
            sizes.append(int(item))
        if ok:
            prefs["splitter_sizes"] = (sizes[0], sizes[1])
            return
    prefs["splitter_sizes"] = None
    diagnostics.append(
        "splitter_sizes: expected two positive ints "
        f"<= {_SPLITTER_MAX}; reset to default")


def _valid_target(value: Any, prefs: dict[str, Any],
                  diagnostics: List[str]) -> None:
    if value is None:  # 缺失 = 静默默认
        prefs["single_view_target"] = SINGLE_VIEW_TIMELINE
        return
    if value in _VALID_TARGETS:
        prefs["single_view_target"] = value
    else:
        prefs["single_view_target"] = SINGLE_VIEW_TIMELINE
        diagnostics.append(
            "single_view_target: expected 'source' or 'timeline', got "
            f"{value!r}; reset to 'timeline'")


def parse_viewport_preferences(
        raw: Any) -> Tuple[ViewportPreferences, List[str]]:
    """解析/迁移/校验；永不抛错，坏项回落默认并给诊断。"""
    diagnostics: List[str] = []

    # 旧扁平键（前端现行两键）→ 迁移进版本化段
    if isinstance(raw, dict) and "version" not in raw and (
            LEGACY_KEY_DUAL_VIEW in raw
            or LEGACY_KEY_SPLITTER_SIZES in raw):
        raw = {
            "version": VIEWPORT_PREFS_VERSION,
            "dual_view": raw.get(LEGACY_KEY_DUAL_VIEW, False),
            "splitter_sizes": raw.get(LEGACY_KEY_SPLITTER_SIZES),
            "single_view_target": raw.get("single_view_target"),
        }
        diagnostics.append("migrated legacy flat viewport keys")

    prefs: dict[str, Any] = {}
    if not isinstance(raw, dict):
        diagnostics.append(
            f"viewport prefs: expected dict, got {type(raw).__name__}; "
            "all defaults applied")
        return ViewportPreferences(), diagnostics

    version = raw.get("version", VIEWPORT_PREFS_VERSION)
    if not isinstance(version, int) or isinstance(version, bool) \
            or version != VIEWPORT_PREFS_VERSION:
        diagnostics.append(
            f"version: unsupported {version!r}; "
            "all defaults applied (single view, Timeline)")
        return ViewportPreferences(), diagnostics

    _valid_bool(raw.get("dual_view", False), "dual_view", prefs,
                diagnostics, default=False)
    _valid_splitter(raw.get("splitter_sizes"), prefs, diagnostics)
    _valid_target(raw.get("single_view_target"), prefs, diagnostics)
    return ViewportPreferences(
        version=VIEWPORT_PREFS_VERSION,
        dual_view=prefs["dual_view"],
        splitter_sizes=prefs["splitter_sizes"],
        single_view_target=prefs["single_view_target"],
    ), diagnostics


def preferences_to_json(prefs: ViewportPreferences) -> str:
    """序列化（配置写盘用；含诊断由宿主另行记录）。"""
    return json.dumps(prefs.to_dict())
