"""B10 / U06：原生 Files 的用户级 UI 配置（QSettings 持久化）。

保存**用户级界面状态**：浏览方式默认值、视图、卡片大小、侧栏/信息栏开合、
排序，以及常用位置/最近访问/检索词（B10）。这些都存在用户自己的 QSettings：
不写入工程包、不随工程分发，换机或打开他人工程不会带出私人路径；
最近访问/固定目录都受数量上限约束。
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QSettings


class NativeFilesPreferences:
    """一层薄封装：把 QSettings 的字符串/字节噪声挡在 UI 代码之外。"""

    ORGANIZATION = "ToposColor"
    APPLICATION = "FilesPanel"
    GROUP = "files/native"

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings or QSettings(self.ORGANIZATION, self.APPLICATION)

    def _key(self, name: str) -> str:
        return f"{self.GROUP}/{name}"

    def _read(self, name: str, default: Any) -> Any:
        value = self._settings.value(self._key(name), default)
        if value is None:
            return default
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(default, int):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        return value

    def _write(self, name: str, value: Any) -> None:
        self._settings.setValue(self._key(name), value)
        self._settings.sync()

    # -- 浏览方式 ------------------------------------------------------------
    @property
    def native_default(self) -> bool:
        """U06：默认进入原生浏览；完整素材管理仍是可切换的兼容入口。

        静态机器/旧用户没有存过该键时按 ``True`` 走原生——这是本轮的入口
        切换决定；只有显式切成"完整素材管理"才会持久化为 False。
        """
        return bool(self._read("native_default", True))

    @native_default.setter
    def native_default(self, value: bool) -> None:
        self._write("native_default", bool(value))

    # -- 常用位置 / 最近访问 / 检索历史（B10） ------------------------------
    MAX_FAVORITES = 12
    MAX_HISTORY = 20
    MAX_SEARCHES = 10

    def _read_list(self, name: str, limit: int) -> list[str]:
        raw = self._settings.value(self._key(name), [])
        if isinstance(raw, str):
            raw = [part for part in raw.split("\n") if part.strip()] if raw else []
        if not isinstance(raw, (list, tuple)):
            return []
        values: list[str] = []
        for item in raw[:limit]:
            text = str(item or "").strip()
            if text and text not in values:
                values.append(text)
        return values

    @property
    def favorites(self) -> list[str]:
        return self._read_list("favorites", self.MAX_FAVORITES)

    @favorites.setter
    def favorites(self, paths: list[str]) -> None:
        self._write("favorites", [str(path) for path in paths][: self.MAX_FAVORITES])

    @property
    def history(self) -> list[str]:
        return self._read_list("history", self.MAX_HISTORY)

    @history.setter
    def history(self, paths: list[str]) -> None:
        self._write("history", [str(path) for path in paths][: self.MAX_HISTORY])

    @property
    def search_history(self) -> list[str]:
        return self._read_list("search_history", self.MAX_SEARCHES)

    @search_history.setter
    def search_history(self, terms: list[str]) -> None:
        self._write("search_history", [str(term) for term in terms][: self.MAX_SEARCHES])

    def remember_location(self, path: str) -> None:
        """最近访问：新位置置顶，重复只留一份；空/根路径不入历史。"""
        text = str(path or "").strip()
        if not text or text == "/":
            return
        existing = [item for item in self.history if item != text]
        self.history = [text] + existing

    @property
    def plugin_pins(self) -> list[str]:
        return self._read_list("plugin_pins", 24)

    @plugin_pins.setter
    def plugin_pins(self, values: list[str]) -> None:
        self._write("plugin_pins", list(dict.fromkeys(values))[:24])

    @property
    def plugin_recent(self) -> list[str]:
        return self._read_list("plugin_recent", 12)

    @plugin_recent.setter
    def plugin_recent(self, values: list[str]) -> None:
        self._write("plugin_recent", list(dict.fromkeys(values))[:12])

    @property
    def plugin_shortcuts(self) -> dict[str, str]:
        raw = self._read("plugin_shortcuts", {})
        if not isinstance(raw, dict):
            return {}
        return {key: value for key, value in raw.items()
                if isinstance(key, str) and isinstance(value, str) and key and value}

    @plugin_shortcuts.setter
    def plugin_shortcuts(self, values: dict[str, str]) -> None:
        self._write("plugin_shortcuts", dict(values))

    def remember_search(self, term: str) -> None:
        text = str(term or "").strip()
        if not text:
            return
        existing = [item for item in self.search_history if item.casefold() != text.casefold()]
        self.search_history = [text] + existing

    # -- 视图状态 ------------------------------------------------------------
    LAYOUT_MODES = ("adaptive", "masonry", "grid", "list")

    @property
    def layout_mode(self) -> str:
        """布局用稳定名称持久化，兼容旧版0=卡片、1=列表的配置。"""
        stored = self._settings.value(self._key("layout_mode"), None)
        if stored in self.LAYOUT_MODES:
            return str(stored)
        if self._settings.contains(self._key("view_index")):
            return "list" if self.view_index == 1 else "grid"
        return "adaptive"

    @layout_mode.setter
    def layout_mode(self, value: str) -> None:
        if value not in self.LAYOUT_MODES:
            raise ValueError(f"Unknown Files layout: {value}")
        self._write("layout_mode", value)
        self.view_index = 1 if value == "list" else 0

    @property
    def grid_fill(self) -> bool:
        return bool(self._read("grid_fill", False))

    @grid_fill.setter
    def grid_fill(self, value: bool) -> None:
        self._write("grid_fill", bool(value))

    @property
    def view_index(self) -> int:
        return int(self._read("view_index", 0))

    @view_index.setter
    def view_index(self, value: int) -> None:
        self._write("view_index", max(0, int(value)))

    @property
    def density(self) -> int:
        return int(self._read("density", 180))

    @density.setter
    def density(self, value: int) -> None:
        self._write("density", int(value))

    @property
    def sidebar_preference(self) -> bool | None:
        """``None`` = 交给宽度自适应；用户手动开合后才落固定值。"""
        stored = self._settings.value(self._key("sidebar_preference"), None)
        if stored is None:
            return None
        if isinstance(stored, str):
            return stored.strip().lower() in ("1", "true", "yes", "on")
        return bool(stored)

    @sidebar_preference.setter
    def sidebar_preference(self, value: bool | None) -> None:
        if value is None:
            self._settings.remove(self._key("sidebar_preference"))
            self._settings.sync()
            return
        self._write("sidebar_preference", bool(value))

    @property
    def inspector_preference(self) -> bool | None:
        """``None`` = 交给宽度自适应（宽窗口才显示信息栏）。"""
        stored = self._settings.value(self._key("inspector_preference"), None)
        if stored is None:
            return None
        if isinstance(stored, str):
            return stored.strip().lower() in ("1", "true", "yes", "on")
        return bool(stored)

    @inspector_preference.setter
    def inspector_preference(self, value: bool | None) -> None:
        if value is None:
            self._settings.remove(self._key("inspector_preference"))
            self._settings.sync()
            return
        self._write("inspector_preference", bool(value))

    @property
    def sort_index(self) -> int:
        return int(self._read("sort_index", 0))

    @sort_index.setter
    def sort_index(self, value: int) -> None:
        self._write("sort_index", max(0, int(value)))
