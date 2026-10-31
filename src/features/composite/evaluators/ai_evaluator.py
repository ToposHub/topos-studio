"""
AIEvaluatorMixin - AI node evaluation methods.

Provides AI-related evaluation methods for nodes that require
async AI computation:
- ai_generate
- ai_inpaint
- ai_expand
- ai_remove
- denoise (AI-based)

Note: The actual AI computation is handled by AIComputeManager
module. This mixin provides the integration hooks.
"""

from typing import Set, TYPE_CHECKING

if TYPE_CHECKING:
    pass


# Node types that require async AI computation
_AI_NODE_TYPES: Set[str] = {"ai_generate", "ai_inpaint", "ai_expand", "ai_remove", "denoise"}


class AIEvaluatorMixin:
    """Mixin providing AI node evaluation methods.

    Note: The actual AI computation is handled by AIComputeManager
    in src/features/composite/render/ai_compute_manager.py. This mixin
    provides integration hooks for the graph evaluator to handle AI nodes
    asynchronously.
    """

    # AI node evaluation is handled by the render scheduler and AIComputeManager
    # This mixin defines the node types that require async AI computation

    @staticmethod
    def get_ai_node_types() -> Set[str]:
        """Get the set of node types that require async AI computation."""
        return _AI_NODE_TYPES