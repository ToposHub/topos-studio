"""Per-image composite isolation: switching images must rebuild the graph.

Regression for the user-reported bug: "on the color page I switch images, then
enter composite, and it still shows the previous image's nodes — the nodes don't
change at all."

Root cause (verified in code, not fakes):

* A ColorOutput graph's COLORNode has ``node_type == "color_input"``
  (src/features/composite/nodes/color_node.py), so once the first image calls
  ``switch_to_color_output_graph()``, ``CompositePage.requires_color_output_input()``
  returns True FOREVER.
* Both entry paths guarded their per-image rebuild with
  ``if page.requires_color_output_input(): return``:
    - FileOpsMixin._update_composite_for_standalone_image (load-time)
    - PageManagementMixin._try_standalone_image_composite (enter-composite)
  That guard short-circuited BEFORE the per-image ``_composite_graph_image_path``
  check, so the rebuild for every subsequent image was unreachable — image B kept
  image A's graph. This is exactly "nodes don't change".

This test models that faithfully: ``requires_color_output_input()`` returns True
once ``switch_to_color_output_graph()`` has run (mirrors the real COLORNode), so the
guard is exercised exactly as in the real app. An earlier version of this test
returned False from requires_color_output_input(), which is why it passed without
catching the bug.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from src.app.controllers.file_controller import FileOpsMixin
from src.app.controllers.page_controller import PageManagementMixin


class _Graph:
    def __init__(self, n):
        self.nodes = list(range(n))  # placeholder "nodes"


class _Session:
    def is_standalone_image(self):
        return True

    def is_timeline_clip(self):
        return False


class _SessionManager:
    def get_current_session(self):
        return _Session()


class _Page:
    """Faithful fake of CompositePage's ColorOutput behaviour.

    requires_color_output_input() mirrors the real method: True iff the graph
    currently holds a color_input node. switch_to_color_output_graph() creates a
    COLORNode (node_type == "color_input"), so it flips the flag on — and the flag
    STAYS on while the user adds nodes on top of that graph, just like the real app.
    """

    def __init__(self):
        self.graph = _Graph(0)
        self.switches = 0
        self._has_color_input = False  # empty graph has no color_input node
        self._current_composite_id = ""

    def set_edit_project(self, project):
        pass

    def requires_color_output_input(self):
        return self._has_color_input

    @property
    def _node_graph(self):
        return self.graph

    def switch_to_color_output_graph(self):
        self.switches += 1
        self.graph = _Graph(2)  # COLOR(ColorOutput) -> Output
        self._has_color_input = True  # COLORNode added → color_input present


def _ctrl():
    return SimpleNamespace(
        _composite_available=True,
        composite_page=_Page(),
        _current_edit_project=None,
        _composite_graph_image_path=None,
        current_file_path=None,
        current_file_is_video=False,
        _color_session_manager=_SessionManager(),
    )


# ---- load-time path: FileOpsMixin._update_composite_for_standalone_image ----

def test_load_time_rebuilds_when_switching_images():
    """load_image(B) must rebuild B's graph even though A left it in ColorOutput mode."""
    ctrl = _ctrl()
    ctrl.current_file_path = "/img/A.png"

    FileOpsMixin._update_composite_for_standalone_image(ctrl, "/img/A.png")
    assert ctrl.composite_page.switches == 1
    assert ctrl._composite_graph_image_path == "/img/A.png"
    assert ctrl.composite_page.requires_color_output_input() is True  # A's graph is ColorOutput

    # User adds nodes to A — graph stays ColorOutput (color_input still present).
    ctrl.composite_page.graph = _Graph(8)

    # Load B: requires_color_output_input() is now True (the buggy guard). Must STILL rebuild.
    ctrl.current_file_path = "/img/B.png"
    FileOpsMixin._update_composite_for_standalone_image(ctrl, "/img/B.png")
    assert ctrl.composite_page.switches == 2, "B must rebuild — not keep A's nodes"
    assert ctrl._composite_graph_image_path == "/img/B.png"
    assert len(ctrl.composite_page._node_graph.nodes) == 2


def test_load_time_preserves_same_image():
    """Re-loading the SAME image must not wipe the current graph."""
    ctrl = _ctrl()
    FileOpsMixin._update_composite_for_standalone_image(ctrl, "/img/A.png")
    assert ctrl.composite_page.switches == 1
    ctrl.composite_page.graph = _Graph(8)  # user's work on A
    FileOpsMixin._update_composite_for_standalone_image(ctrl, "/img/A.png")
    assert ctrl.composite_page.switches == 1, "same image must not rebuild"
    assert len(ctrl.composite_page._node_graph.nodes) == 8


def test_load_time_restored_graph_is_kept():
    """A just-restored saved graph must be kept and bound to the image."""
    ctrl = _ctrl()
    ctrl.composite_page.graph = _Graph(5)  # a restored graph
    FileOpsMixin._update_composite_for_standalone_image(ctrl, "/img/A.png", restored_composite=True)
    assert ctrl.composite_page.switches == 0, "restored graph must not be rebuilt"
    assert ctrl._composite_graph_image_path == "/img/A.png"
    assert len(ctrl.composite_page._node_graph.nodes) == 5


# ---- enter-composite path: PageManagementMixin._try_standalone_image_composite ----

def test_enter_composite_rebuilds_when_switching_images():
    """Entering composite for image B must rebuild, not keep A's ColorOutput graph."""
    ctrl = _ctrl()
    ctrl.current_file_path = "/img/A.png"
    assert PageManagementMixin._try_standalone_image_composite(ctrl) is True
    assert ctrl.composite_page.switches == 1
    assert ctrl._composite_graph_image_path == "/img/A.png"
    assert ctrl.composite_page.requires_color_output_input() is True

    # Switch to B; the in-memory graph is still A's ColorOutput graph.
    ctrl.current_file_path = "/img/B.png"
    assert PageManagementMixin._try_standalone_image_composite(ctrl) is True
    assert ctrl.composite_page.switches == 2, "B must rebuild — not keep A's nodes"
    assert ctrl._composite_graph_image_path == "/img/B.png"


def test_enter_composite_preserves_same_image():
    """Re-entering composite for the same image must not rebuild."""
    ctrl = _ctrl()
    ctrl.current_file_path = "/img/A.png"
    PageManagementMixin._try_standalone_image_composite(ctrl)
    assert ctrl.composite_page.switches == 1
    PageManagementMixin._try_standalone_image_composite(ctrl)
    assert ctrl.composite_page.switches == 1, "same image must not rebuild"
