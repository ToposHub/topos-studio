"""
Node Plugin System - Dynamic node type registration and discovery.

Provides a plugin architecture for extending the node graph with custom node types.

Key Components:
- NodePlugin: Abstract base class for node plugins
- NodePluginManager: Discovers and registers plugins from multiple sources

Plugin Discovery Paths (in order):
1. ~/.topos/plugins/node_plugins/ - User-level plugins
2. <app_dir>/plugins/node_plugins/ - Application-level plugins

Plugin Structure:
Each plugin module must define a subclass of NodePlugin that:
- Declares the node types it provides
- Returns the Node subclass instances
- Specifies category and metadata
"""

import importlib.util
import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type, TYPE_CHECKING

if TYPE_CHECKING:
    from ..nodes.base_node import Node

logger = logging.getLogger(__name__)


@dataclass
class PluginInfo:
    """Metadata about a loaded plugin."""
    name: str
    version: str = "1.0.0"
    author: str = ""
    description: str = ""
    path: str = ""  # Source file path
    enabled: bool = True
    error: Optional[str] = None  # Load error if any


class NodePlugin(ABC):
    """Abstract base class for node plugins.

    A plugin can provide one or more node types. Subclasses must implement
    the abstract methods to declare and create their nodes.

    Example:
        class MyFilterPlugin(NodePlugin):
            @classmethod
            def plugin_info(cls) -> PluginInfo:
                return PluginInfo(
                    name="my_filter",
                    version="1.0.0",
                    author="Developer",
                    description="Custom filter nodes"
                )

            @classmethod
            def node_types(cls) -> List[Type["Node"]]:
                from .my_filter_node import MyFilterNode
                return [MyFilterNode]

            @classmethod
            def category(cls) -> str:
                return "Filter"
    """

    @classmethod
    @abstractmethod
    def plugin_info(cls) -> PluginInfo:
        """Return plugin metadata.

        Returns:
            PluginInfo with name, version, author, description, etc.
        """
        pass

    @classmethod
    @abstractmethod
    def node_types(cls) -> List[Type["Node"]]:
        """Return list of Node subclasses this plugin provides.

        Returns:
            List of Node subclass types (not instances).
        """
        pass

    @classmethod
    def category(cls) -> str:
        """Return the category for nodes in this plugin.

        Override this to place nodes in a specific category in the UI.
        Default is "Plugins" for all plugin nodes.

        Returns:
            Category name string.
        """
        return "Plugins"

    @classmethod
    def on_load(cls) -> bool:
        """Called when the plugin is loaded.

        Override for initialization tasks like loading resources,
        checking dependencies, or registering callbacks.

        Returns:
            True if initialization succeeded, False to abort loading.
        """
        return True

    @classmethod
    def on_unload(cls) -> None:
        """Called when the plugin is unloaded.

        Override for cleanup tasks like releasing resources.
        """
        pass

    @classmethod
    def dependencies(cls) -> List[str]:
        """Return list of required Python packages.

        Override to declare external dependencies. The plugin manager
        will log warnings if dependencies are missing.

        Returns:
            List of package names (e.g., ["numpy", "pillow"]).
        """
        return []

    @classmethod
    def check_dependencies(cls) -> tuple:
        """Check if all declared dependencies are available.

        Returns:
            Tuple of (all_available: bool, missing: List[str])
        """
        missing = []
        for pkg in cls.dependencies():
            try:
                __import__(pkg)
            except ImportError:
                missing.append(pkg)
        return (len(missing) == 0, missing)


class NodePluginManager:
    """Manages discovery, loading, and registration of node plugins.

    Plugin Sources (searched in order):
    1. User plugins: ~/.topos/plugins/node_plugins/
    2. App plugins: <app_root>/plugins/node_plugins/

    Thread Safety:
    - Registration methods are not thread-safe. Call from main thread only.
    - Discovery performs no locking of its own (AUD-5204: an earlier
      revision of this docstring claimed a filesystem lock that does not
      exist) — treat it like registration and call it from a single
      thread.

    Example:
        manager = NodePluginManager()
        manager.discover_plugins()

        # Get all registered node types
        node_types = manager.get_node_types()

        # Register callback for new plugins
        manager.on_plugin_loaded = lambda plugin: print(f"Loaded: {plugin.plugin_info().name}")
    """

    # Plugin discovery directories (searched in order)
    PLUGIN_DIRS = [
        # User-level plugins (highest priority)
        Path.home() / ".topos" / "plugins" / "node_plugins",
        # Application-level plugins: the repository/application root.
        # AUD-5200: this file lives at <root>/src/features/composite/core/,
        # so the app root is parents[4] — the previous parent³ chain resolved
        # to <root>/src/features/plugins/node_plugins, which does not exist,
        # and the bundled node plugins were never discovered.
        Path(__file__).resolve().parents[4] / "plugins" / "node_plugins",
    ]

    def __init__(self) -> None:
        self._plugins: Dict[str, Type[NodePlugin]] = {}  # plugin_name -> plugin_class
        self._node_types: Dict[str, Type["Node"]] = {}    # node_type -> Node class
        self._node_categories: Dict[str, List[str]] = {} # category -> [node_types]
        self._plugin_errors: Dict[str, str] = {}          # plugin_name -> error message
        self._loaded_modules: Dict[str, Any] = {}         # module_path -> module object
        # AUD-5202: node_type -> 被 override 的 (Node class, category) 历史栈。
        # unregister 恢复前一持有者，避免误删仍处注册状态的插件节点。
        self._node_type_overridden: Dict[str, List[tuple[Type["Node"], str]]] = {}

        # Callbacks
        self.on_plugin_loaded: Optional[Callable[[Type[NodePlugin]], None]] = None
        self.on_plugin_failed: Optional[Callable[[str, str], None]] = None
        self.on_node_registered: Optional[Callable[[str, Type["Node"]], None]] = None
        # N-13（2026-10-14 审计）：注册表同步钩子。加载/卸载后由
        # node_engine 侧（register_plugin_nodes 安装）把 manager 的
        # node_types 增量同步进全局 NODE_REGISTRY/NODE_CATEGORIES——
        # 旧实现卸载只清 manager 本地注册，核心注册表与 factory 仍能
        # 创建已卸载插件的节点。
        self.on_registry_sync: Optional[Callable[[], None]] = None

    @property
    def plugins(self) -> Dict[str, Type[NodePlugin]]:
        """Get all loaded plugins."""
        return dict(self._plugins)

    @property
    def node_types(self) -> Dict[str, Type["Node"]]:
        """Get all registered node types from plugins."""
        return dict(self._node_types)

    @property
    def node_categories(self) -> Dict[str, List[str]]:
        """Get node categories from plugins."""
        return dict(self._node_categories)

    @property
    def errors(self) -> Dict[str, str]:
        """Get plugin loading errors."""
        return dict(self._plugin_errors)

    def discover_plugins(self) -> int:
        """Discover and load all plugins from plugin directories.

        Searches all plugin directories and loads valid plugin modules.
        Returns the total number of plugins successfully loaded.

        Returns:
            Number of successfully loaded plugins.
        """
        loaded_count = 0

        for plugin_dir in self.PLUGIN_DIRS:
            if not plugin_dir.exists():
                logger.debug(f"Plugin directory does not exist: {plugin_dir}")
                continue

            logger.info(f"Scanning plugin directory: {plugin_dir}")

            # Find all .py files (excluding __init__.py and _private.py)
            for plugin_file in plugin_dir.glob("*.py"):
                if plugin_file.stem.startswith("_"):
                    continue

                if self._load_plugin_file(plugin_file):
                    loaded_count += 1

            # Also check subdirectories as plugin packages
            for sub_dir in plugin_dir.iterdir():
                if sub_dir.is_dir() and not sub_dir.name.startswith("_"):
                    init_file = sub_dir / "__init__.py"
                    if init_file.exists():
                        if self._load_plugin_file(init_file, is_package=True):
                            loaded_count += 1

        logger.info(f"Plugin discovery complete: {loaded_count} plugins loaded")
        return loaded_count

    def _load_plugin_file(self, file_path: Path, is_package: bool = False) -> bool:
        """Load a single plugin file and register its plugins.

        Args:
            file_path: Path to the plugin .py file or __init__.py
            is_package: True if loading a package directory

        Returns:
            True if at least one plugin was loaded successfully.
        """
        module_name = f"topos_node_plugin_{file_path.stem}"
        if is_package:
            module_name = f"topos_node_plugin_{file_path.parent.name}"

        # Skip if already loaded
        if module_name in self._loaded_modules:
            logger.debug(f"Plugin module already loaded: {module_name}")
            return False

        try:
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot create module spec for {file_path}")

            module = importlib.util.module_from_spec(spec)
            # AUD-5201: 记录同名旧条目，exec_module 失败时回退 sys.modules，
            # 避免半初始化模块永久遗留全局模块表（importlib 官方模式）。
            previous_module = sys.modules.get(module_name)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                if previous_module is not None:
                    sys.modules[module_name] = previous_module
                else:
                    sys.modules.pop(module_name, None)
                raise

            # Find all NodePlugin subclasses in the module
            plugins_found = []
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type) and
                    issubclass(attr, NodePlugin) and
                    attr is not NodePlugin):
                    plugins_found.append(attr)

            if not plugins_found:
                logger.warning(f"No NodePlugin subclass found in {file_path}")
                return False

            # Register each plugin found
            success = False
            for plugin_class in plugins_found:
                if self.register_plugin(plugin_class, str(file_path)):
                    success = True

            if success:
                self._loaded_modules[module_name] = module

            return success

        except Exception as e:
            error_msg = f"Failed to load plugin from {file_path}: {e}"
            logger.error(error_msg, exc_info=True)
            self._plugin_errors[file_path.stem] = str(e)

            if self.on_plugin_failed:
                self.on_plugin_failed(file_path.stem, str(e))

            return False

    def register_plugin(self, plugin_class: Type[NodePlugin], source_path: str = "") -> bool:
        """Register a plugin and all its node types.

        Args:
            plugin_class: The NodePlugin subclass to register.
            source_path: Path to the plugin source file (for error reporting).

        Returns:
            True if registration succeeded.
        """
        try:
            # Get plugin info
            info = plugin_class.plugin_info()

            # Check if already registered
            if info.name in self._plugins:
                logger.warning(f"Plugin '{info.name}' already registered, skipping")
                return False

            # Check dependencies
            all_deps_available, missing = plugin_class.check_dependencies()
            if not all_deps_available:
                error_msg = f"Plugin '{info.name}' missing dependencies: {missing}"
                logger.error(error_msg)
                self._plugin_errors[info.name] = error_msg

                if self.on_plugin_failed:
                    self.on_plugin_failed(info.name, error_msg)

                return False

            # Call on_load hook
            if not plugin_class.on_load():
                error_msg = f"Plugin '{info.name}' on_load() returned False"
                logger.error(error_msg)
                self._plugin_errors[info.name] = error_msg

                if self.on_plugin_failed:
                    self.on_plugin_failed(info.name, error_msg)

                return False

            # Get node types
            node_types = plugin_class.node_types()
            if not node_types:
                logger.warning(f"Plugin '{info.name}' provides no node types")
                return False

            # AUD-5203: 先完成全部读取/校验（任何属性缺失在此暴露），
            # 再进入一次性提交区，避免半程失败留下无归属的幽灵节点类型。
            category = plugin_class.category()
            staged: List[tuple[str, Type["Node"]]] = []
            for node_class in node_types:
                node_type = node_class.node_type
                if not isinstance(node_type, str) or not node_type:
                    raise TypeError(
                        f"Plugin '{info.name}' declares invalid node_type: "
                        f"{node_type!r} on {node_class!r}")
                staged.append((node_type, node_class))

            # AUD-5203: 提交区快照，异常时整体回滚注册表变更。
            snapshot_types: Dict[str, Type["Node"]] = dict(self._node_types)
            snapshot_categories: Dict[str, List[str]] = {
                cat: list(types) for cat, types in self._node_categories.items()}
            snapshot_overridden: Dict[str, List[tuple[Type["Node"], str]]] = {
                nt: list(history) for nt, history in self._node_type_overridden.items()}

            try:
                for node_type, node_class in staged:
                    if node_type in self._node_types:
                        logger.warning(
                            f"Node type '{node_type}' already registered, "
                            f"overriding with '{info.name}'"
                        )
                        # AUD-5202: 记录被 override 的前一持有者，注销时恢复。
                        self._node_type_overridden.setdefault(node_type, []).append(
                            (self._node_types[node_type],
                             self._category_of_node_type(node_type) or category))

                    self._node_types[node_type] = node_class

                    self._node_categories.setdefault(category, [])
                    # AUD-5203: 分类 append 前判重，重试不再产生重复项。
                    if node_type not in self._node_categories[category]:
                        self._node_categories[category].append(node_type)
                    # override 场景下从旧分类移除，避免跨分类重复列出。
                    for other_cat, other_types in self._node_categories.items():
                        if other_cat != category and node_type in other_types:
                            other_types.remove(node_type)

                    logger.info(f"Registered node type '{node_type}' from plugin '{info.name}'")

                    if self.on_node_registered:
                        self.on_node_registered(node_type, node_class)

                # Store plugin（与节点类型同处提交区，保证原子性）
                self._plugins[info.name] = plugin_class
            except Exception:
                self._node_types = snapshot_types
                self._node_categories = snapshot_categories
                self._node_type_overridden = snapshot_overridden
                raise

            # Update plugin info
            info.path = source_path
            info.enabled = True

            logger.info(f"Plugin '{info.name}' v{info.version} loaded successfully")

            if self.on_plugin_loaded:
                # 注册已提交完成；通知回调自身异常不回滚已提交状态。
                try:
                    self.on_plugin_loaded(plugin_class)
                except Exception as e:
                    logger.error(
                        f"on_plugin_loaded callback failed for '{info.name}': {e}",
                        exc_info=True)

            return True

        except Exception as e:
            plugin_name = getattr(plugin_class, "__name__", "unknown")
            error_msg = f"Failed to register plugin {plugin_name}: {e}"
            logger.error(error_msg, exc_info=True)
            self._plugin_errors[plugin_name] = str(e)

            if self.on_plugin_failed:
                self.on_plugin_failed(plugin_name, str(e))

            return False

    def _category_of_node_type(self, node_type: str) -> Optional[str]:
        """Return the category currently listing *node_type*, or None."""
        for cat, types in self._node_categories.items():
            if node_type in types:
                return cat
        return None

    def unregister_plugin(self, plugin_name: str) -> bool:
        """Unregister a plugin and its node types.

        Node types whose registration has been overridden by another plugin
        are left intact (and the previous holder is restored on unregister of
        the overrider), so unloading one plugin never deletes node types that
        a still-registered plugin provides.

        Args:
            plugin_name: Name of the plugin to unregister.

        Returns:
            True if the plugin was found and unregistered.
        """
        if plugin_name not in self._plugins:
            logger.warning(f"Plugin '{plugin_name}' not found")
            return False

        plugin_class = self._plugins[plugin_name]

        try:
            # Call on_unload hook
            plugin_class.on_unload()

            # Remove node types
            category = plugin_class.category()

            for node_class in plugin_class.node_types():
                node_type = node_class.node_type

                if self._node_types.get(node_type) is not node_class:
                    # AUD-5202: 当前持有者是其他插件（override 注册），
                    # 不得无差别删除仍处注册状态的节点类型。
                    logger.info(
                        f"Node type '{node_type}' is currently provided by "
                        f"another plugin; keeping current registration")
                    continue

                # AUD-5202: 恢复被本插件 override 的前一持有者（如有）。
                history = self._node_type_overridden.get(node_type)
                if history:
                    previous_class, previous_category = history.pop()
                    if not history:
                        del self._node_type_overridden[node_type]
                    self._node_types[node_type] = previous_class
                    # 分类归位：从当前分类移除，放回前一分类；
                    # 顺带清掉因此变空的分类，避免残留空列表。
                    for cat in list(self._node_categories):
                        types = self._node_categories[cat]
                        if node_type in types:
                            types.remove(node_type)
                        if not types:
                            del self._node_categories[cat]
                    self._node_categories.setdefault(previous_category, [])
                    if node_type not in self._node_categories[previous_category]:
                        self._node_categories[previous_category].append(node_type)
                    logger.info(
                        f"Node type '{node_type}' restored to previous "
                        f"registration")
                    continue

                del self._node_types[node_type]

                if category in self._node_categories:
                    if node_type in self._node_categories[category]:
                        self._node_categories[category].remove(node_type)

                    # Remove empty category
                    if not self._node_categories[category]:
                        del self._node_categories[category]

            # AUD-5202: 清除本插件类在 override 历史栈中的残留条目，
            # 避免后续注销时恢复一个已卸载插件的类。
            unloaded_classes = set(plugin_class.node_types())
            for history in self._node_type_overridden.values():
                history[:] = [
                    entry for entry in history if entry[0] not in unloaded_classes]

            # Remove plugin
            del self._plugins[plugin_name]

            # N-13：卸载后同步核心注册表（若宿主安装了同步钩子）。
            if self.on_registry_sync is not None:
                try:
                    self.on_registry_sync()
                except Exception:
                    logger.exception("registry sync after plugin unload failed")

            logger.info(f"Plugin '{plugin_name}' unregistered")
            return True

        except Exception as e:
            logger.error(f"Error unregistering plugin '{plugin_name}': {e}")
            return False

    def get_plugin_for_node(self, node_type: str) -> Optional[Type[NodePlugin]]:
        """Get the plugin that provides a specific node type.

        Args:
            node_type: The node type identifier.

        Returns:
            The plugin class or None if not found.
        """
        if node_type not in self._node_types:
            return None

        node_class = self._node_types[node_type]

        for plugin_class in self._plugins.values():
            if node_class in plugin_class.node_types():
                return plugin_class

        return None

    def get_plugin_info(self, plugin_name: str) -> Optional[PluginInfo]:
        """Get info for a specific plugin.

        Args:
            plugin_name: The plugin name.

        Returns:
            PluginInfo or None if not found.
        """
        if plugin_name not in self._plugins:
            return None

        return self._plugins[plugin_name].plugin_info()

    def get_all_plugin_info(self) -> Dict[str, PluginInfo]:
        """Get info for all loaded plugins.

        Returns:
            Dict mapping plugin name to PluginInfo.
        """
        return {
            name: cls.plugin_info()
            for name, cls in self._plugins.items()
        }

    def create_node(self, node_type: str, **kwargs) -> Optional["Node"]:
        """Create a node instance by type.

        Args:
            node_type: The node type identifier.
            **kwargs: Additional arguments passed to the node constructor.

        Returns:
            Node instance or None if type not found.
        """
        if node_type not in self._node_types:
            return None

        node_class = self._node_types[node_type]
        return node_class(**kwargs)

    def reload_plugins(self) -> int:
        """Reload all plugins.

        Useful during development to pick up plugin changes.

        Returns:
            Number of plugins loaded after reload.
        """
        # Unregister all plugins
        for plugin_name in list(self._plugins.keys()):
            self.unregister_plugin(plugin_name)

        # Clear loaded modules cache
        self._loaded_modules.clear()
        self._plugin_errors.clear()

        # Re-discover
        return self.discover_plugins()


# Singleton instance for convenience
_manager_instance: Optional[NodePluginManager] = None


def get_plugin_manager() -> NodePluginManager:
    """Get the global plugin manager instance.

    Creates the instance on first call.

    Returns:
        The global NodePluginManager instance.
    """
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = NodePluginManager()
    return _manager_instance


def discover_and_register_plugins() -> int:
    """Discover and register all plugins using the global manager.

    Convenience function that calls get_plugin_manager().discover_plugins().

    Returns:
        Number of plugins loaded.
    """
    return get_plugin_manager().discover_plugins()
