"""Color workspace presets (P1-COLOR-19).

调色页工作区预设：一键切换一组面板可见性（可选附带 scope 输入源）。

设计约束：
- 内置预设（Primary / Secondary / HDR / Tracking / Scopes / Delivery）只读，
  用户自定义预设持久化在 ConfigManager 的 ``color_workspaces`` 键下，
  与 LayoutStore 的 ``layout_profiles``（页面几何状态）互不覆盖——切换工作区
  只更新当前 Color 页布局，不污染用户全局布局，反之亦然。
- 预设中引用的面板 id 在应用时按 PanelRegistry 现状过滤（升级/裁剪功能后
  旧预设自动迁移为可用的子集，绝不引用不存在的面板）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence
from uuid import uuid4

CONFIG_KEY = "color_workspaces"

logger = logging.getLogger(__name__)

VALID_SCOPE_SOURCES = ("graded", "source", "reference")

# Color 页常驻面板：任何工作区都保留（视频/图像模式差异由
# _enforce_page_dock_visibility 在应用后兜底）。
_ALWAYS_VISIBLE = frozenset({"workspace.viewport"})


@dataclass(frozen=True)
class ColorWorkspacePreset:
    """一个 Color 工作区预设。

    Attributes:
        preset_id: 稳定标识。内置预设为 ``builtin.*``，用户预设为 ``user.*``。
        name: 显示名（用户预设）或 i18n 兜底名（内置预设）。
        visible_panels: 该工作区可见的 panel_id 集合。
        scope_source: 可选的 scope 输入源（'graded'/'source'/'reference'），
            空字符串表示不改变。
        builtin: 是否内置预设（不可删除）。
    """

    preset_id: str
    name: str
    visible_panels: frozenset = field(default_factory=frozenset)
    scope_source: str = ""
    builtin: bool = False
    title_key: str = ""

    def display_name(self, tr: Optional[Callable[..., str]] = None) -> str:
        """Resolve display name, preferring i18n for builtin presets."""
        if self.title_key and tr is not None:
            translated = tr(self.title_key, self.name)
            if translated and translated != self.title_key:
                return translated
        return self.name


def builtin_color_workspaces() -> List[ColorWorkspacePreset]:
    """Return the builtin workspace presets (DaVinci 风格任务导向布局)."""
    return [
        ColorWorkspacePreset(
            preset_id="builtin.primary",
            name="Primary",
            title_key="workspace.primary",
            builtin=True,
            visible_panels=frozenset({
                "workspace.viewport",
                "color.timeline_clips",
                "workspace.presets",
                "workspace.layers",
                "color.grading",
                "color.curves",
                "color.lut",
            }),
        ),
        ColorWorkspacePreset(
            preset_id="builtin.secondary",
            name="Secondary",
            title_key="workspace.secondary",
            builtin=True,
            visible_panels=frozenset({
                "workspace.viewport",
                "color.timeline_clips",
                "color.qualifier",
                "color.masks",
                "color.mapping",
                "color.tracker",
            }),
        ),
        ColorWorkspacePreset(
            preset_id="builtin.hdr",
            name="HDR",
            title_key="workspace.hdr",
            builtin=True,
            visible_panels=frozenset({
                "workspace.viewport",
                "color.timeline_clips",
                "color.raw",
                "color.soft_clip",
                "color.grading",
                "color.lut",
                "color.analysis",
            }),
            scope_source="graded",
        ),
        ColorWorkspacePreset(
            preset_id="builtin.tracking",
            name="Tracking",
            title_key="workspace.tracking",
            builtin=True,
            visible_panels=frozenset({
                "workspace.viewport",
                "color.timeline_clips",
                "color.tracker",
                "color.masks",
                "color.analysis",
            }),
        ),
        ColorWorkspacePreset(
            preset_id="builtin.scopes",
            name="Scopes",
            title_key="workspace.scopes",
            builtin=True,
            visible_panels=frozenset({
                "workspace.viewport",
                "color.timeline_clips",
                "color.analysis",
                "workspace.info",
            }),
        ),
        ColorWorkspacePreset(
            preset_id="builtin.delivery",
            name="Delivery",
            title_key="workspace.delivery",
            builtin=True,
            visible_panels=frozenset({
                "workspace.viewport",
                "color.timeline_clips",
                "workspace.export",
                "color.analysis",
                "workspace.info",
            }),
        ),
    ]


def _preset_to_dict(preset: ColorWorkspacePreset) -> Dict[str, Any]:
    return {
        "preset_id": preset.preset_id,
        "name": preset.name,
        "visible_panels": sorted(preset.visible_panels),
        "scope_source": preset.scope_source,
    }


def _preset_from_dict(data: Dict[str, Any]) -> Optional[ColorWorkspacePreset]:
    """Parse a user preset entry; returns None for malformed entries."""
    if not isinstance(data, dict):
        return None
    preset_id = data.get("preset_id")
    name = data.get("name")
    if not isinstance(preset_id, str) or not preset_id.startswith("user."):
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    panels = data.get("visible_panels", [])
    if not isinstance(panels, (list, tuple)):
        return None  # 结构损坏的条目直接丢弃
    # 与保存路径一致：加载即保证 viewer 常驻（手写/迁移数据可能缺）
    visible = frozenset(
        p for p in panels if isinstance(p, str)) | _ALWAYS_VISIBLE
    scope = data.get("scope_source", "")
    if scope not in VALID_SCOPE_SOURCES:
        scope = ""
    return ColorWorkspacePreset(
        preset_id=preset_id,
        name=name.strip(),
        visible_panels=visible,
        scope_source=scope,
        builtin=False,
    )


def _read_user_workspaces(config: Any) -> Optional[List[ColorWorkspacePreset]]:
    """Read user workspaces from config.

    AUD-1005: 返回值区分"读取失败"（``None``）与"确实没有数据"（空列表）。
    写回路径（save/delete）在读取失败时必须拒绝整体覆盖——否则一次瞬态
    ConfigManager.get 失败（IO/锁/损坏）会让后续保存基于空列表重建并
    永久清空全部用户预设。
    """
    raw = None
    if config is not None:
        try:
            raw = config.get(CONFIG_KEY)
        except Exception:
            logger.warning(
                "Failed to read user workspaces from config key %r; "
                "write-back paths must refuse to overwrite",
                CONFIG_KEY,
                exc_info=True,
            )
            return None
    if isinstance(raw, dict):
        raw = raw.get("items")
    if not isinstance(raw, (list, tuple)):
        return []
    presets: List[ColorWorkspacePreset] = []
    seen_ids: set[str] = set()
    for entry in raw:
        preset = _preset_from_dict(entry)
        if preset is None or preset.preset_id in seen_ids:
            continue
        seen_ids.add(preset.preset_id)
        presets.append(preset)
    return presets


def load_user_workspaces(config: Any) -> List[ColorWorkspacePreset]:
    """Load user workspaces from ConfigManager (tolerant of bad entries).

    读取本身失败（而非数据为空/损坏条目）时返回空列表但会记录日志；
    需要区分两种情况的写回路径应使用 :func:`_read_user_workspaces`。
    """
    presets = _read_user_workspaces(config)
    return presets if presets is not None else []


def _write_user_workspaces(
    config: Any, presets: Sequence[ColorWorkspacePreset]
) -> bool:
    """Persist the full user-workspace list. Returns False on failure.

    AUD-1006: 写入失败必须留下日志痕迹并让调用方感知，避免
    ``save_user_workspace`` 仍返回新 preset 造成"保存成功"假象。
    """
    if config is None:
        return False
    try:
        config.set(CONFIG_KEY, {"items": [_preset_to_dict(p) for p in presets]})
    except Exception:
        logger.exception(
            "Failed to write user workspaces to config key %r", CONFIG_KEY)
        return False
    return True


def save_user_workspace(
    config: Any,
    name: str,
    visible_panels: Sequence[str],
    scope_source: str = "",
) -> Optional[ColorWorkspacePreset]:
    """Save (or rename) a user workspace. Same-name entries are replaced.

    读取已有预设失败（fail-safe）或写回失败时返回 ``None``——调用方以
    非 None 返回值当作保存成功，因此绝不能在失败时仍返回 preset。
    """
    clean_name = (name or "").strip()
    if not clean_name:
        return None
    if scope_source not in VALID_SCOPE_SOURCES:
        scope_source = ""
    existing = _read_user_workspaces(config)
    if existing is None:
        # AUD-1005: 读取失败时拒绝基于空列表整体覆盖写回。
        logger.warning(
            "save_user_workspace(%r): refusing to save — existing presets "
            "could not be read from config", clean_name)
        return None
    panels = frozenset(_ALWAYS_VISIBLE | set(visible_panels))
    presets = [p for p in existing if p.name != clean_name]
    preset = ColorWorkspacePreset(
        preset_id=f"user.{uuid4().hex[:12]}",
        name=clean_name,
        visible_panels=panels,
        scope_source=scope_source,
    )
    presets.append(preset)
    if not _write_user_workspaces(config, presets):
        return None
    return preset


def delete_user_workspace(config: Any, preset_id: str) -> bool:
    """Delete a user workspace by id. Builtin presets are never deletable."""
    if not isinstance(preset_id, str) or not preset_id.startswith("user."):
        return False
    existing = _read_user_workspaces(config)
    if existing is None:
        # AUD-1005: 读取失败时拒绝整体覆盖写回（删除同样基于全量重建）。
        logger.warning(
            "delete_user_workspace(%r): refusing to delete — existing "
            "presets could not be read from config", preset_id)
        return False
    remaining = [p for p in existing if p.preset_id != preset_id]
    if len(remaining) == len(existing):
        return False
    if not _write_user_workspaces(config, remaining):
        return False
    return True


def find_workspace(
    config: Any, preset_id: str
) -> Optional[ColorWorkspacePreset]:
    """Look up a workspace by id among builtin + user presets."""
    for preset in builtin_color_workspaces():
        if preset.preset_id == preset_id:
            return preset
    for preset in load_user_workspaces(config):
        if preset.preset_id == preset_id:
            return preset
    return None


def sanitize_preset(
    preset: ColorWorkspacePreset,
    registered_panel_ids: Sequence[str],
    work_mode: str = "all",
) -> frozenset:
    """Resolve a preset's panel list against the live panel registry.

    - drops panel ids that no longer exist (upgrade migration);
    - promotes legacy HSL Curves/Mixer/Warper ids to Color Mapping;
    - drops panels disabled for the current work mode
      (video: image-only Library/export; image: timeline clips);
    - keeps the viewport always visible.
    """
    registered = set(registered_panel_ids)
    resolved = set(preset.visible_panels) & registered

    # The three tools used to be independent workspace panels.  Preserve old
    # saved workspaces by promoting any of their ids to the combined panel.
    if (
        "color.mapping" in registered
        and set(preset.visible_panels)
        & {"color.hsl_curves", "color.mixer", "color.warper"}
    ):
        resolved.add("color.mapping")
    if work_mode == "video":
        resolved -= {"workspace.library", "workspace.export"}
    elif work_mode == "image":
        resolved -= {"color.timeline_clips"}
    resolved |= set(_ALWAYS_VISIBLE)
    return frozenset(resolved)
