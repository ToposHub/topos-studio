"""Regression tests for audit fixes batch A (w2-g4).

Covers three Critical findings from docs/audit/2026-09-28/batches/
batch-054-features-composite-core.md and the image-loader thread-safety
audit:

- AUD-5400: TilePaddingComputer source-type determination is data-driven.
  Graphs rooted at video_frame / text / ramp / noise (and any other
  source or generator not in the old hand-written _SOURCE_TYPES set)
  must no longer compute total_padding == 0.
- AUD-5401: _get_node_padding resolves node classes through
  NODE_REGISTRY instead of a hand-written if/elif import chain, so
  denoise / face_smooth / light_wrap / edge_detect (and every other
  effect node declaring padding) return their real padding.
- AUD-48007: ImageLoader.cancel_all keeps a strong reference
  (zombie list) to decode workers that outlive the bounded wait, so a
  running QThread can never lose its last Python reference and be
  destroyed by the GC mid-run.
"""

from types import SimpleNamespace

import pytest

from src.features.composite.core.image_loader import ImageLoader
from src.features.composite.core.tile_padding_computer import (
    TilePaddingComputer,
    _needs_tile_padding_check,
)

pytestmark = pytest.mark.composite_core


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _ns(node_id: str, node_type: str, params=None,
        inputs: list = None) -> "SimpleNamespace":
    """Build a NodeSnapshot stand-in (duck-typed, like the audit tests)."""
    return SimpleNamespace(
        node_id=node_id,
        node_type=node_type,
        parameters=dict(params or {}),
        disabled=False,
        input_ports=list(inputs or []),
        output_ports=[("result", "image")],
    )


def _conn(src: str, dst: str, src_port: str = "result",
          dst_port: str = "image") -> tuple:
    return (src, src_port, dst, dst_port)


def _make_snapshot(nodes, connections, eval_order):
    """Build a minimal GraphSnapshot via the real dataclass."""
    from src.features.composite.core.graph_snapshot import (
        GraphSnapshot, NodeSnapshot, ConnectionSnapshot,
    )
    node_snapshots = {}
    for ns in nodes:
        node_snapshots[ns.node_id] = NodeSnapshot(
            node_id=ns.node_id,
            node_type=ns.node_type,
            parameters=ns.parameters,
            disabled=False,
            input_ports=list(ns.input_ports or [("image", "image")]),
            output_ports=[("result", "image")],
        )
    return GraphSnapshot(
        generation=1,
        nodes=node_snapshots,
        connections=[
            ConnectionSnapshot(s, sp, d, dp) for (s, sp, d, dp) in connections
        ],
        eval_order=list(eval_order),
        color_texture_id=0,
        color_width=1920,
        color_height=1080,
        color_revision=0,
    )


# ═══════════════════════════════════════════════════════════════════════
# AUD-5400: data-driven source/root determination
# ═══════════════════════════════════════════════════════════════════════

class TestAud5400DataDrivenSources:
    """total_padding must not depend on a hand-written source-type list."""

    @pytest.mark.parametrize("root_type", [
        "video_frame",   # media source (was missing from _SOURCE_TYPES)
        "text",          # generator source (was missing)
        "ramp",          # generator, action_type="effect" (was missing)
        "noise",         # generator, action_type="effect" (was missing)
        "particle",      # source (was missing)
        "image_input",   # legacy recognised source (must keep working)
        "color_input",   # deprecated but still deserialisable source
    ])
    def test_rooted_blur_chain_has_padding(self, root_type):
        # root -> blur(radius=10) -> output : total must be >= 10
        snapshot = _make_snapshot(
            nodes=[
                _ns("root1", root_type, inputs=[]),
                _ns("blur1", "blur", {"radius": 10.0}),
            ],
            connections=[_conn("root1", "blur1")],
            eval_order=["root1", "blur1"],
        )
        info = TilePaddingComputer.compute(snapshot)
        assert info.total_padding >= 10, (
            f"graph rooted at {root_type!r} lost its padding requirement "
            f"(total={info.total_padding})")

    def test_generator_root_with_downstream_effects_exact(self):
        # ramp -> blur(10) -> erode_dilate(size=5 -> 5//2+1=3) -> output
        snapshot = _make_snapshot(
            nodes=[
                _ns("ramp1", "ramp", inputs=[]),
                _ns("blur1", "blur", {"radius": 10.0}),
                _ns("erode1", "erode_dilate", {"size": 5}),
            ],
            connections=[_conn("ramp1", "blur1"), _conn("blur1", "erode1")],
            eval_order=["ramp1", "blur1", "erode1"],
        )
        info = TilePaddingComputer.compute(snapshot)
        assert info.total_padding == 13

    def test_mixed_media_and_generator_graphs_take_max(self):
        # image_input chain needs 0, video_frame chain needs 25:
        # the total must cover the deepest chain, whichever root feeds it.
        snapshot = _make_snapshot(
            nodes=[
                _ns("img1", "image_input", inputs=[]),
                _ns("vid1", "video_frame", inputs=[]),
                _ns("grade1", "grade", {}),
                _ns("blur1", "blur", {"radius": 25.0}),
            ],
            connections=[
                _conn("img1", "grade1"),
                _conn("vid1", "blur1"),
            ],
            eval_order=["img1", "vid1", "grade1", "blur1"],
        )
        info = TilePaddingComputer.compute(snapshot)
        assert info.total_padding >= 25

    def test_legacy_source_chain_value_unchanged(self):
        # Locks the pre-existing (correct) behaviour for recognised
        # sources: color_input -> blur(10) -> erode_dilate(3) == 13.
        snapshot = _make_snapshot(
            nodes=[
                _ns("color1", "color_input", inputs=[]),
                _ns("blur1", "blur", {"radius": 10.0}),
                _ns("erode1", "erode_dilate", {"size": 5}),
            ],
            connections=[_conn("color1", "blur1"), _conn("blur1", "erode1")],
            eval_order=["color1", "blur1", "erode1"],
        )
        info = TilePaddingComputer.compute(snapshot)
        assert info.total_padding == 13

    def test_padding_chain_tracks_deepest_chain(self):
        snapshot = _make_snapshot(
            nodes=[
                _ns("ramp1", "ramp", inputs=[]),
                _ns("blur1", "blur", {"radius": 10.0}),
            ],
            connections=[_conn("ramp1", "blur1")],
            eval_order=["ramp1", "blur1"],
        )
        info = TilePaddingComputer.compute(snapshot)
        assert info.total_padding == 10
        assert "blur1" in info.padding_chain


# ═══════════════════════════════════════════════════════════════════════
# AUD-5401: registry-driven _get_node_padding
# ═══════════════════════════════════════════════════════════════════════

class TestAud5401RegistryDrivenNodePadding:
    """Effect nodes must report their declared/dynamic padding."""

    def test_denoise_padding(self):
        # _tile_padding returns int(radius); default radius 3.
        assert TilePaddingComputer._get_node_padding(
            _ns("d1", "denoise", {"radius": 6})) == 6
        assert TilePaddingComputer._get_node_padding(
            _ns("d2", "denoise", {})) == 3

    def test_face_smooth_padding(self):
        # _tile_padding returns int(radius); default radius 4.
        assert TilePaddingComputer._get_node_padding(
            _ns("fs1", "face_smooth", {"radius": 7})) == 7
        assert TilePaddingComputer._get_node_padding(
            _ns("fs2", "face_smooth", {})) == 4

    def test_light_wrap_padding(self):
        # No _tile_padding: legacy requires_roi_expansion returns
        # max(0, int(width)); default width 12.
        assert TilePaddingComputer._get_node_padding(
            _ns("lw1", "light_wrap", {"width": 20.0})) == 20
        assert TilePaddingComputer._get_node_padding(
            _ns("lw2", "light_wrap", {})) == 12

    def test_edge_detect_padding(self):
        # max(1, kernel_size // 2 + preblur*2); defaults: 3//2 = 1.
        assert TilePaddingComputer._get_node_padding(
            _ns("ed1", "edge_detect", {"kernel_size": 7})) == 3
        assert TilePaddingComputer._get_node_padding(
            _ns("ed2", "edge_detect", {})) == 1

    def test_capability_declared_padding_types_are_covered(self):
        """Anti-omission lock: every capability-table effect type that
        declares non-zero tile_padding must resolve to a node class with
        a padding method — no type may fall back to a silent 0 because
        it is missing from an import chain."""
        from src.features.composite.core.node_capabilities import (
            get_all_capabilities,
        )
        table = get_all_capabilities()
        declared = {
            t for t, cap in table.items()
            if cap.action_type == "effect" and cap.tile_padding > 0
        }
        assert declared, "capability table lost its padding declarations"

        for node_type in sorted(declared):
            assert _needs_tile_padding_check(node_type), node_type
            cls = TilePaddingComputer._resolve_node_class(node_type)
            assert cls is not None, (
                f"{node_type} declares tile_padding but no class resolves")
            inst = cls.__new__(cls)
            inst.__init__(node_id="probe")
            assert hasattr(inst, "_tile_padding") or hasattr(
                inst, "requires_roi_expansion"), node_type

    def test_every_effect_type_matches_direct_instance_padding(self):
        """Dispatch parity: _get_node_padding must equal what the node
        class itself computes, for EVERY registered effect type.  This is
        the regression lock for the removed if/elif import chain: a new
        effect node with padding can never be silently dropped again."""
        from src.features.composite.core.node_capabilities import (
            get_all_capabilities,
        )
        from src.features.composite.core.node_engine import NODE_REGISTRY

        table = get_all_capabilities()
        checked = 0
        for node_type, cap in sorted(table.items()):
            if cap.action_type != "effect":
                continue
            cls = NODE_REGISTRY.get(node_type)
            if not isinstance(cls, type):
                continue  # deferred/plugin-registered types
            inst = cls.__new__(cls)
            inst.__init__(node_id="probe")
            if hasattr(inst, "_tile_padding"):
                expected = max(0, int(inst._tile_padding({})))
            elif hasattr(inst, "requires_roi_expansion"):
                expected = max(0, int(inst.requires_roi_expansion({})))
            else:
                expected = 0
            got = TilePaddingComputer._get_node_padding(
                _ns("probe", node_type, {}))
            assert got == expected, (
                f"{node_type}: registry dispatch returned {got}, "
                f"node class computes {expected}")
            checked += 1
        assert checked >= 30, (
            f"parity check covered only {checked} effect types; the "
            "capability table is expected to cover far more")

    def test_denoise_chain_reaches_total_padding(self):
        """End-to-end: a video_frame -> denoise(radius=6) chain must
        produce total_padding >= 6 (both fixes working together)."""
        snapshot = _make_snapshot(
            nodes=[
                _ns("vid1", "video_frame", inputs=[]),
                _ns("dn1", "denoise", {"radius": 6}),
            ],
            connections=[_conn("vid1", "dn1")],
            eval_order=["vid1", "dn1"],
        )
        info = TilePaddingComputer.compute(snapshot)
        assert info.total_padding == 6


# ═══════════════════════════════════════════════════════════════════════
# AUD-48007: cancel_all zombie keep-alive
# ═══════════════════════════════════════════════════════════════════════

class _FakeWorker:
    """Duck-typed ImageDecodeWorker double for cancel_all behaviour.

    Implements exactly the surface cancel_all touches: cancel(), wait(),
    isRunning() and objectName().
    """

    def __init__(self, name: str = "fake", running: bool = True,
                 wait_result: bool = False):
        self._name = name
        self._running = running
        self._wait_result = wait_result
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def wait(self, ms=None) -> bool:
        return self._wait_result

    def isRunning(self) -> bool:
        return self._running

    def objectName(self) -> str:
        return self._name


class TestAud48007CancelAllKeepAlive:
    def test_stuck_worker_keeps_strong_reference(self):
        loader = ImageLoader()
        stuck = _FakeWorker("stuck", running=True, wait_result=False)
        loader._active_workers["node1"] = stuck

        loader.cancel_all()

        assert stuck.cancelled
        # AUD-48007: the timed-out worker must remain strongly referenced
        # so the GC cannot destroy a running QThread mid-run.
        assert stuck in loader._zombie_workers
        assert loader._active_workers == {}

    def test_finished_worker_is_released(self):
        loader = ImageLoader()
        done = _FakeWorker("done", running=False, wait_result=True)
        loader._active_workers["node1"] = done

        loader.cancel_all()

        assert done not in loader._zombie_workers
        assert loader._cancelled_workers == []
        assert loader._zombie_workers == []

    def test_zombie_list_pruned_once_worker_exits(self):
        loader = ImageLoader()
        zombie = _FakeWorker("z", running=True, wait_result=False)
        loader._active_workers["node1"] = zombie
        loader.cancel_all()
        assert zombie in loader._zombie_workers

        # The worker eventually finishes; the next cleanup pass (decode
        # completion / failure / cancel) must prune the keep-alive entry.
        zombie._running = False
        loader._mutex.lock()
        try:
            loader._cleanup_finished_workers_unlocked()
        finally:
            loader._mutex.unlock()
        assert loader._zombie_workers == []

    def test_cancel_all_keeps_workers_cancelled_during_wait(self):
        """Workers appended while cancel_all waits must not be cleared
        by the post-wait bookkeeping (the old clear() dropped them)."""
        loader = ImageLoader()
        stuck = _FakeWorker("stuck", running=True, wait_result=False)
        during_wait = _FakeWorker("late", running=True, wait_result=False)
        loader._active_workers["node1"] = stuck

        original_wait = stuck.wait

        def _wait_then_register(ms=None):
            # Simulate another thread cancelling a new worker while
            # cancel_all is waiting with the mutex released.
            loader._active_workers["node2"] = during_wait
            loader._cancel_worker_unlocked("node2")
            return original_wait(ms)

        stuck.wait = _wait_then_register
        loader.cancel_all()

        assert stuck in loader._zombie_workers
        assert loader._active_workers == {}
        # The worker cancelled mid-wait has not been through a bounded
        # wait yet: it must remain strongly referenced in the cancelled
        # list (the old unconditional clear() dropped it while running).
        assert during_wait in loader._cancelled_workers

        # The next cancel pass gives it its bounded wait and parks it in
        # the keep-alive list when it still refuses to stop.
        loader.cancel_all()
        assert during_wait in loader._zombie_workers
        assert loader._cancelled_workers == []
        assert loader._zombie_workers.count(stuck) == 1

    def test_repeated_cancel_all_does_not_duplicate_zombies(self):
        loader = ImageLoader()
        stuck = _FakeWorker("stuck", running=True, wait_result=False)
        loader._active_workers["node1"] = stuck

        loader.cancel_all()
        loader.cancel_all()

        assert loader._zombie_workers.count(stuck) == 1
