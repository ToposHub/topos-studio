"""G11：Gallery 显示偏好的版本化持久化（用户级、不进工程文件）。

- 偏好段独立于工程数据（不把 QPixmap／缓存／机器绝对路径写进任何工程
  文件），落在 Gallery 根下 ``gallery_preferences.json``（原子写盘）。
- **版本化**：``schema`` 字段显式；未知未来版本的文件按默认值加载且
  **不清除**用户文件（等升级路径），损坏 JSON 同样只回退本次默认。
- **旧键迁移**：``presets_columns``（列数 2–6）只能映射成合理缩略图宽
  度默认——绝不把“列数 2”读成 2px。
- 范围钳制：缩略图宽度 108–256（与 GalleryGridController 口径一致），
  view_mode 只认 grid/list，排序只认声明的键。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PREFERENCES_SCHEMA = 1
PREFERENCES_FILENAME = "gallery_preferences.json"

THUMB_WIDTH_MIN = 108
THUMB_WIDTH_MAX = 256
VIEW_MODES = ("grid", "list")
SORT_KEYS = ("name", "label", "created_at")

#: 旧 presets_columns（列数）→ 合理默认缩略图宽度
_LEGACY_COLUMNS_TO_WIDTH = {2: 192, 3: 168, 4: 148, 5: 128, 6: 112}
#: 默认宽度＝chrome 既有默认（108）：无偏好文件时行为与旧版逐项一致
_DEFAULT_WIDTH = 108


@dataclass
class GalleryPreferences:
    """用户级显示偏好（版本化、可校验）。"""

    schema: int = PREFERENCES_SCHEMA
    thumbnail_width: int = _DEFAULT_WIDTH
    view_mode: str = "grid"           # grid / list
    sort_desc: bool = False           # 名称升/降序（chrome 现有口径）
    sidebar_visible: bool = True

    def __post_init__(self) -> None:
        self.thumbnail_width = _clamp(self.thumbnail_width,
                                      THUMB_WIDTH_MIN, THUMB_WIDTH_MAX,
                                      _DEFAULT_WIDTH)
        if self.view_mode not in VIEW_MODES:
            self.view_mode = "grid"
        self.sort_desc = bool(self.sort_desc)
        self.sidebar_visible = bool(self.sidebar_visible)


def _clamp(value: int, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def preferences_path(presets_root: str) -> str:
    return os.path.join(presets_root, PREFERENCES_FILENAME)


def load_preferences(presets_root: str) -> GalleryPreferences:
    """读取偏好；损坏/未来 schema/越界值 → 本次默认（不动用户文件）。"""
    path = preferences_path(presets_root)
    if not os.path.isfile(path):
        return GalleryPreferences()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return GalleryPreferences()
        schema = data.get("schema", 0)
        if not isinstance(schema, int) or schema > PREFERENCES_SCHEMA:
            # 未来版本：不猜字段、不覆盖文件
            return GalleryPreferences()
        return GalleryPreferences(
            schema=PREFERENCES_SCHEMA,
            thumbnail_width=data.get("thumbnail_width",
                                     _DEFAULT_WIDTH),
            view_mode=str(data.get("view_mode", "grid")),
            sort_desc=bool(data.get("sort_desc", False)),
            sidebar_visible=bool(data.get("sidebar_visible", True)))
    except (OSError, ValueError):
        logger.debug("gallery preferences: unreadable (%s); using defaults",
                     path, exc_info=True)
        return GalleryPreferences()


def save_preferences(presets_root: str,
                     prefs: GalleryPreferences) -> bool:
    """原子写盘（tmp＋replace）；失败返回 False 不抛。

    根目录必须已存在（面板负责创建）——服务不静默建深层路径。
    """
    path = preferences_path(presets_root)
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        if not os.path.isdir(presets_root):
            return False
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(prefs), fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def migrate_legacy_columns(columns: Any) -> int:
    """旧 ``presets_columns`` 列数 → 合理缩略图宽度默认（永不 2px）。"""
    try:
        number = int(columns)
    except (TypeError, ValueError):
        return _DEFAULT_WIDTH
    return _LEGACY_COLUMNS_TO_WIDTH.get(number, _DEFAULT_WIDTH)
