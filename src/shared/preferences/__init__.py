"""Preference page contribution system for modular settings.

Each feature module contributes PreferencePageDescriptors to describe its
settings pages. The PreferencesDialog builds its UI dynamically from all
registered descriptors, filtering by enabled features.

ModularPreferencePage protocol:
    Feature modules can implement ModularPreferencePage to own their
    preference page's widget creation, load, apply, and defaults logic
    entirely, instead of relying on centralized PreferencesDialog methods.
"""

from __future__ import annotations

import threading

from dataclasses import dataclass
from typing import Callable, Any, Optional, Protocol, TYPE_CHECKING

from src.shared.config.config_manager import ConfigManager

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QWidget

__all__ = [
    "ModularPreferencePage",
    "PreferencePageDescriptor",
    "PreferencesRegistry",
    "get_config_manager",
]

# 进程级 ConfigManager 单例（AUD-36700）。消费方（如 composite 磁盘缓存
# ``disk_cache_store``）以 ``from src.shared.preferences import
# get_config_manager`` 取配置管理器——此前该符号未导出，ImportError 被
# 消费方的兜底 except 吞掉，``composite.node_cache_format`` 等偏好设置
# 全部静默失效。惰性单例 + 双检锁：首次调用才读盘，多线程取同一实例。
_CONFIG_MANAGER_LOCK = threading.Lock()
_CONFIG_MANAGER_INSTANCE: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """返回进程级 ``ConfigManager`` 单例（线程安全，惰性创建）。

    供偏好读取方（磁盘缓存策略、各 feature 的偏好键查询）取得与
    UI ``PreferencesDialog`` 一致的配置入口；``ConfigManager`` 自身负责
    原子读写与损坏降级。
    """
    global _CONFIG_MANAGER_INSTANCE
    if _CONFIG_MANAGER_INSTANCE is None:
        with _CONFIG_MANAGER_LOCK:
            if _CONFIG_MANAGER_INSTANCE is None:
                _CONFIG_MANAGER_INSTANCE = ConfigManager()
    return _CONFIG_MANAGER_INSTANCE


class ModularPreferencePage(Protocol):
    """A preference page that owns its own widget creation, load, apply, and defaults logic.

    Feature modules implement this protocol to contribute self-contained
    preference pages. When a PreferencePageDescriptor has a page_object,
    the PreferencesDialog delegates load/apply/defaults to it instead of
    handling those settings centrally.
    """

    @property
    def page_id(self) -> str:
        """Unique identifier for this preference page."""
        ...

    def create_widget(self, parent: "QWidget") -> "QWidget":
        """Create and return the preference page widget.

        Args:
            parent: Parent widget (the PreferencesDialog).

        Returns:
            The constructed page widget.
        """
        ...

    def load(self, config: dict) -> None:
        """Load settings from config dict into widgets.

        Args:
            config: The full configuration dictionary.
        """
        ...

    def apply(self, config_manager: Any) -> None:
        """Read widget values and write them to the config manager.

        Args:
            config_manager: ConfigManager instance with set() and save_config().
        """
        ...

    def restore_defaults(self, defaults: dict) -> None:
        """Reset widgets to default values.

        Args:
            defaults: The DEFAULT_CONFIG dictionary from ConfigManager.
        """
        ...


@dataclass
class PreferencePageDescriptor:
    """Describes a preference page that can be contributed by a feature module.

    Attributes:
        page_id: Unique identifier, e.g. "color.processing"
        title: Display text for the page/category
        category: Category group for sidebar navigation, e.g. "Color"
        order: Sort order within the category (lower = first)
        feature_id: Feature that contributes this page. None = app-level.
        factory: Callable that creates the preference page QWidget.
                 Must be lazy — only called when user opens the page.
        page_object: Optional ModularPreferencePage instance. When set,
                     the factory is built from page_object.create_widget()
                     and load/apply/defaults are delegated to it.
    """

    page_id: str
    title: str
    category: str
    factory: Optional[Callable[[Any], Any]] = None
    order: int = 100
    feature_id: Optional[str] = None
    page_object: Optional[Any] = None

    def __post_init__(self):
        """If page_object is set but no factory, create factory from page_object."""
        if self.page_object is not None and self.factory is None:
            page_obj = self.page_object

            def _factory(parent=None):
                return page_obj.create_widget(parent)

            self.factory = _factory


class PreferencesRegistry:
    """Registry for preference page contributions from feature modules.

    Collects PreferencePageDescriptors and provides filtered views
    based on currently enabled features.
    """

    def __init__(self):
        self._pages: dict[str, PreferencePageDescriptor] = {}

    def register(self, descriptor: PreferencePageDescriptor) -> None:
        """Register a preference page descriptor."""
        if descriptor.page_id in self._pages:
            return  # Duplicate, ignore
        self._pages[descriptor.page_id] = descriptor

    def get_pages(
        self, enabled_features: set[str] | None = None
    ) -> list[PreferencePageDescriptor]:
        """Get all preference pages, optionally filtered by enabled features.

        Args:
            enabled_features: Set of enabled feature IDs. If None, return all.

        Returns:
            Sorted list of PreferencePageDescriptors.
        """
        pages = []
        for page in self._pages.values():
            if enabled_features is not None:
                if page.feature_id is not None and page.feature_id not in enabled_features:
                    continue
            pages.append(page)
        return sorted(pages, key=lambda p: (p.order, p.category, p.page_id))

    def get_categories(
        self, enabled_features: set[str] | None = None
    ) -> list[str]:
        """Get unique categories, sorted."""
        pages = self.get_pages(enabled_features)
        seen = set()
        result = []
        for p in pages:
            if p.category not in seen:
                seen.add(p.category)
                result.append(p.category)
        return result

    def get_page(self, page_id: str) -> Optional[PreferencePageDescriptor]:
        """Get a specific page by ID."""
        return self._pages.get(page_id)
