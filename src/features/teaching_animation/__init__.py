"""Optional Teaching Animation feature module.

The package intentionally keeps its Phase 0 surface small.  Host features
should import the stable facade rather than anything below ``core`` or
``render``.
"""

from .module import TeachingAnimationModule
from .scene_store import TeachingSceneStore, get_shared_scene_store

__all__ = ["TeachingAnimationModule", "TeachingSceneStore", "get_shared_scene_store"]
