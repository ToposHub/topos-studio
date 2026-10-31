"""
Node Plugins - Custom node types for the Topos Color node graph.

This directory contains plugins that extend the node graph with custom
node types. Each plugin should define a NodePlugin subclass that
provides one or more Node types.

Plugin Discovery:
- Plugins are automatically discovered by NodePluginManager
- Place .py files here or create subdirectories with __init__.py
- Plugin files should not start with underscore (_private.py are ignored)

Example Plugin Structure:
    posterize_plugin.py:
        class PosterizePlugin(NodePlugin):
            @classmethod
            def plugin_info(cls) -> PluginInfo:
                return PluginInfo(
                    name="posterize",
                    version="1.0.0",
                    author="Your Name",
                    description="Posterize effect node"
                )

            @classmethod
            def node_types(cls) -> List[Type[Node]]:
                from .posterize_node import PosterizeNode
                return [PosterizeNode]

            @classmethod
            def category(cls) -> str:
                return "Filter"

For more information, see:
- src/composite/node_plugin.py for the NodePlugin base class
- src/composite/node_engine.py for the registration API
"""

# This directory is scanned by NodePluginManager.discover_plugins()
# Individual plugin modules are loaded dynamically