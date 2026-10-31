"""
NodeState - Enumeration of possible node states in the composite graph.

States:
    DIRTY        - Needs re-evaluation (params or inputs changed)
    PROCESSING   - Async computation in progress (AI nodes)
    CACHED       - Result available from disk cache
    FAILED       - Last computation failed
    BYPASSED     - Node is disabled, pass-through
    PLACEHOLDER  - Node type not yet implemented
"""

from enum import Enum, auto


class NodeState(Enum):
    DIRTY = auto()
    PROCESSING = auto()
    CACHED = auto()
    FAILED = auto()
    BYPASSED = auto()
    PLACEHOLDER = auto()
