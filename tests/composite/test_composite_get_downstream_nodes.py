"""Regression: CompositePage._get_downstream_nodes must traverse the graph.

Root cause: ``_get_downstream_nodes`` (reached from the prefetch path that fires
after every parameter change / reset) read
``self._node_graph.get_connections()``, but ``NodeGraph`` exposes connections as
a plain attribute ``self.connections`` — there is no ``get_connections()``
method. Resetting a parameter re-evaluated the graph, the prefetch timer fired,
``_trigger_prefetch_request`` called ``_get_downstream_nodes``, and the app
crashed with::

    AttributeError: 'NodeGraph' object has no attribute 'get_connections'.
                    Did you mean: 'connections'?

Fix: read ``self._node_graph.connections``.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode
from src.features.composite.nodes.grade_node import GradeNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.ui.composite_page import CompositePage


def test_get_downstream_nodes_traverses_chain(qapp):
    """_get_downstream_nodes must walk the graph's connections without crashing.

    Before the fix this raised AttributeError on ``get_connections()`` every time
    the prefetch path ran after a parameter reset.
    """
    page = CompositePage(parent=None)
    try:
        graph = page._node_graph
        source = TimelineClipInputNode()
        grade = GradeNode()
        output = OutputNode()
        graph.add_node(source)
        graph.add_node(grade)
        graph.add_node(output)
        graph.connect(source.id, "output", grade.id, "image")
        graph.connect(grade.id, "result", output.id, "image")

        # source -> grade -> output : downstream of source is grade then output
        down_from_source = page._get_downstream_nodes(source.id)
        assert grade.id in down_from_source
        assert output.id in down_from_source
        # BFS keeps topological order: grade appears before output
        assert down_from_source.index(grade.id) < down_from_source.index(output.id)

        # downstream of grade is only output
        assert page._get_downstream_nodes(grade.id) == [output.id]
        # output has no downstream
        assert page._get_downstream_nodes(output.id) == []
    finally:
        page.deleteLater()
