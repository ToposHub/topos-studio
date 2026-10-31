"""
Thumbnail throttling policy for CompositeRenderer node thumbnail requests.

Controls how aggressively node thumbnails are requested after each frame render.
GPU readback is expensive, so this policy ensures:
- Main view rendering always takes priority over thumbnail readback.
- During playback, thumbnail updates are suspended entirely.
- During parameter interaction (drag), only the selected node is updated at reduced fps.
- During idle, visible nodes are updated at a controlled rate.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class CompositeThumbnailPolicy:
    """Policy controlling thumbnail request behaviour.

    Attributes:
        enabled: Master switch.  When False, no thumbnail readbacks happen.
        update_during_playback: Allow thumbnail updates while playback is active.
            Defaults to False because playback rendering must not be starved.
        max_updates_per_second_idle: Max thumbnail batches per second when idle.
        max_updates_per_second_interaction: Max thumbnail batches per second when
            the user is actively dragging a parameter.
        max_nodes_per_tick: Max number of nodes whose thumbnails are requested
            in a single tick (batch).
        selected_first: When True, the selected node is always requested first
            in each tick, before other visible nodes.
    """

    enabled: bool = True
    update_during_playback: bool = False
    max_updates_per_second_idle: int = 10
    max_updates_per_second_interaction: int = 5
    max_nodes_per_tick: int = 4
    selected_first: bool = True


@dataclass
class ThumbnailThrottleState:
    """Mutable throttle state owned by CompositePage.

    Separated from the policy so the policy can be swapped without losing state.
    """

    last_request_at: float = 0.0
    pending_nodes: Set[str] = field(default_factory=set)
    playback_suspended: bool = False
    last_cache_key: Dict[str, str] = field(default_factory=dict)


def compute_thumbnail_requests(
    policy: CompositeThumbnailPolicy,
    state: ThumbnailThrottleState,
    *,
    is_interacting: bool,
    render_pending: bool,
    visible_node_ids: List[str],
    selected_node_id: Optional[str],
    renderer_cache_keys: Dict[str, str],
    now: Optional[float] = None,
) -> List[str]:
    """Pure function that decides which nodes need thumbnail readback.

    Returns a list of node IDs to request, in priority order.
    Returns an empty list if the request should be skipped this tick.

    This function is free of side-effects and safe to unit-test without
    any GUI or GPU imports.

    Args:
        policy: The active thumbnail policy.
        state: The mutable throttle state (only read, not mutated).
        is_interacting: True when the user is actively dragging a parameter.
        render_pending: True when a frame render is still in-flight.
        visible_node_ids: Node IDs currently visible in the editor viewport.
        selected_node_id: The currently selected node ID, or None.
        renderer_cache_keys: Current cache keys from the renderer
            (node_id -> cache_key).
        now: Current time from time.monotonic().  Injected for testability.
    """
    if not policy.enabled:
        return []

    # ── a) Playback suspension ────────────────────────────────────────
    if state.playback_suspended and not policy.update_during_playback:
        return []

    # ── b) Throttle interval ──────────────────────────────────────────
    if now is None:
        now = time.monotonic()

    if is_interacting:
        min_interval = 1.0 / max(policy.max_updates_per_second_interaction, 1)
    else:
        min_interval = 1.0 / max(policy.max_updates_per_second_idle, 1)

    elapsed = now - state.last_request_at
    if elapsed < min_interval:
        return []

    # ── c) Main-view priority ─────────────────────────────────────────
    if render_pending:
        return []

    # ── d) Build candidate node list (priority order) ─────────────────
    ordered: List[str] = []
    if selected_node_id:
        ordered.append(selected_node_id)
    for nid in visible_node_ids:
        if nid not in ordered:
            ordered.append(nid)

    # ── e) During interaction: only the selected node ─────────────────
    if is_interacting:
        if selected_node_id:
            return [selected_node_id]
        return []

    # ── f) Cache-key skip ─────────────────────────────────────────────
    nodes_to_request: List[str] = []
    for nid in ordered:
        new_key = renderer_cache_keys.get(nid)
        old_key = state.last_cache_key.get(nid)
        if new_key is not None and new_key == old_key:
            continue  # output unchanged
        nodes_to_request.append(nid)
        if len(nodes_to_request) >= policy.max_nodes_per_tick:
            break

    return nodes_to_request
