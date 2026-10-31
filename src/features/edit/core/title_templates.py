"""C1: 用户自定义标题模板存储（Presets/User/title_templates/）。

schema 与 :class:`CaptionStylePreset` 子集一致（preset_id/label/category/
style/description），原子写、坏文件容错跳过（与 B6 自定义导出预设同模式）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from src.features.edit.core.captions.model import CaptionStyle
from src.features.edit.core.captions.style_preset import CaptionStylePreset

logger = logging.getLogger(__name__)

__all__ = [
    "title_templates_dir",
    "list_user_title_templates",
    "save_title_template",
    "delete_title_template",
    "rename_title_template",
]

_SLUG_RE = re.compile(r"[^0-9A-Za-z_-]+")


def title_templates_dir() -> str:
    """用户标题模板目录（不存在则创建）。"""
    path = os.path.join(os.getcwd(), "Presets", "User", "title_templates")
    os.makedirs(path, exist_ok=True)
    return path


def _slug(name: str) -> str:
    slug = _SLUG_RE.sub("_", name.strip()).strip("_")
    return slug or "template"


def _template_id_for_name(name: str, existing: List[str]) -> str:
    base = f"user_{_slug(name)}"
    candidate = base
    counter = 2
    while candidate in existing:
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def _parse_template(data: Dict[str, Any], source: str) -> Optional[CaptionStylePreset]:
    """解析单个模板 JSON；坏内容返回 None 并记日志。"""
    try:
        label = str(data.get("label") or "").strip()
        if not label:
            raise ValueError("missing label")
        style = CaptionStyle.from_dict(dict(data.get("style") or {}))
        category = data.get("category")
        category = str(category) if category else None
        preset_id = str(data.get("preset_id") or "").strip()
        return CaptionStylePreset(
            preset_id=preset_id,
            label=label,
            style=style,
            description=str(data.get("description") or ""),
            is_builtin=False,
            category=category,
        )
    except Exception as exc:  # noqa: BLE001 — 单个坏文件不拖垮列表
        logger.warning("Skipping bad title template %s: %s", source, exc)
        return None


def list_user_title_templates() -> List[CaptionStylePreset]:
    """加载全部用户标题模板（文件名序，稳定）。"""
    directory = title_templates_dir()
    try:
        filenames = sorted(os.listdir(directory))
    except OSError:
        return []
    presets: List[CaptionStylePreset] = []
    for filename in filenames:
        if not filename.endswith(".json"):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            logger.warning("Skipping unreadable title template %s: %s", path, exc)
            continue
        preset = _parse_template(data, path)
        if preset is not None:
            presets.append(preset)
    return presets


def save_title_template(
    name: str,
    style: CaptionStyle,
    category: Optional[str] = None,
    description: str = "",
) -> CaptionStylePreset:
    """保存用户标题模板；同名拒绝（查重）。"""
    clean = (name or "").strip()
    if not clean:
        raise ValueError("template name is empty")
    existing = list_user_title_templates()
    for preset in existing:
        if preset.label == clean:
            raise ValueError(f"duplicate template name: {clean}")
    taken = [p.preset_id for p in existing]
    base_id = f"user_{_slug(clean)}"
    preset_id = base_id
    counter = 2
    while preset_id in taken:
        preset_id = f"{base_id}_{counter}"
        counter += 1
    payload = {
        "preset_id": preset_id,
        "label": clean,
        "category": category,
        "description": description,
        "created_at": time.time(),
        "is_custom": True,
        "style": style.to_dict() if hasattr(style, "to_dict") else vars(style),
    }
    path = os.path.join(title_templates_dir(), f"{preset_id}.json")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    logger.info("Saved title template %r → %s", clean, path)
    return CaptionStylePreset(
        preset_id=preset_id,
        label=clean,
        style=style,
        description=description,
        is_builtin=False,
        category=category,
    )


def delete_title_template(preset_id: str) -> bool:
    """删除用户标题模板；返回是否删除了文件。"""
    safe = _slug(preset_id)
    path = os.path.join(title_templates_dir(), f"{safe}.json")
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Failed to delete title template %s: %s", preset_id, exc)
        return False


def rename_title_template(preset_id: str, new_name: str) -> CaptionStylePreset:
    """F2.9：重命名用户标题模板（样式/类别/描述不变，换文件；同名拒绝）。"""
    preset = next(
        (p for p in list_user_title_templates() if p.preset_id == preset_id),
        None)
    if preset is None:
        raise ValueError(f"template not found: {preset_id}")
    renamed = save_title_template(
        new_name, preset.style, category=preset.category,
        description=preset.description)
    delete_title_template(preset_id)
    return renamed
