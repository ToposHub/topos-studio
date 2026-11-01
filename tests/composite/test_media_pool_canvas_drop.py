"""AUD-10503 页面侧接线：媒体池素材拖到合成节点画布 → asset-backed 节点。

The widget-level half (drag accept + ``assets_dropped`` emit) is covered in
``test_fix_wd4g25_aud.py``; that batch explicitly left the page-side consumer
unwired (see docs/plans/2026-09-28-multi-machine-program/fixes/reports/
wd4-g25.md —「composite_page 尚无消费者创建 asset-backed 节点」), which made
the drop look accepted while nothing happened.  These tests drive the real
``dropEvent`` → signal → ``CompositePage`` handler path end to end (headless,
offscreen): image/video assets create ``image_input``/``video_frame`` nodes,
audio is rejected (AUD-5004), unknown ids are skipped, and one drag is a
single undo step.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Iterator, List, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PyQt6.QtCore import QMimeData, QPointF, Qt
from PyQt6.QtGui import QDropEvent

from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.node_editor_widget import (
    MIME_TYPE_MEDIA_POOL_ASSETS,
)


# ── fakes ──────────────────────────────────────────────────────────────


class _Asset:
    def __init__(self, asset_id: str, path: str, media_type: str) -> None:
        self.asset_id = asset_id
        self.path = path
        self.media_type = media_type
        self.name = ""


class _Project:
    """EditProject stand-in exposing the ``assets`` lookup only."""

    def __init__(self, *assets: _Asset) -> None:
        self.assets: List[_Asset] = list(assets)


@pytest.fixture
def page(qapp) -> Iterator[CompositePage]:
    page = CompositePage()
    page.set_edit_project(_Project(
        _Asset("asset-img", "/tmp/drop/a.png", "image"),
        _Asset("asset-vid", "/tmp/drop/b.mp4", "video"),
        _Asset("asset-aud", "/tmp/drop/c.wav", "audio"),
    ))
    # Bind the editor's actions object to the page's live graph (the ctor
    # builds from its own empty composition; rebind defensively).
    page._node_editor.build_from_graph(page._node_graph)
    yield page
    page.close()


def _drop_pool_assets(page: CompositePage, asset_ids: List[str],
                      view_pos: QPointF = QPointF(40.0, 40.0)) -> None:
    """Deliver a real media-pool QDropEvent to the node canvas."""
    mime = QMimeData()
    mime.setData(MIME_TYPE_MEDIA_POOL_ASSETS,
                 json.dumps(asset_ids).encode("utf-8"))
    event = QDropEvent(view_pos, Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    page._node_editor.dropEvent(event)


def _source_nodes(page: CompositePage) -> List[tuple]:
    """(node_type, file_path) of the source nodes the drop created."""
    return [
        (n.node_type, n.parameters.get("file_path"))
        for n in page._node_graph.nodes.values()
        if n.node_type in ("image_input", "video_frame", "model3d")
    ]


# ── tests ──────────────────────────────────────────────────────────────


def test_pool_drop_creates_asset_backed_nodes(page: CompositePage) -> None:
    """Drop image+video+audio ids → image_input/video_frame nodes only."""
    _drop_pool_assets(page, ["asset-img", "asset-vid", "asset-aud"])

    sources = sorted(_source_nodes(page))
    # Audio is not layer-renderable (AUD-5004) — no node for it.
    assert sources == [
        ("image_input", "/tmp/drop/a.png"),
        ("video_frame", "/tmp/drop/b.mp4"),
    ], f"unexpected source nodes: {sources}"


def test_pool_drop_offsets_multiple_nodes(page: CompositePage) -> None:
    """Multiple assets spread horizontally (220px), not stacked."""
    _drop_pool_assets(page, ["asset-img", "asset-vid"])

    nodes = [n for n in page._node_graph.nodes.values()
             if n.node_type in ("image_input", "video_frame")]
    assert len(nodes) == 2
    xs = sorted(n.position[0] for n in nodes)
    assert xs[1] - xs[0] == pytest.approx(220.0)
    # Same row.
    assert {round(n.position[1], 3) for n in nodes} == {
        round(nodes[0].position[1], 3)}


def test_pool_drop_is_single_undo_step(page: CompositePage) -> None:
    """One drag = one undo group; a single Ctrl+Z removes every created node."""
    before = len(page._node_graph.nodes)
    _drop_pool_assets(page, ["asset-img", "asset-vid"])
    assert len(page._node_graph.nodes) == before + 2

    stack = page._undo_stack
    assert stack is not None
    stack.undo()
    assert len(page._node_graph.nodes) == before, (
        "one undo step must remove every node created by the drag")


def test_pool_drop_unknown_ids_are_skipped(page: CompositePage) -> None:
    """Unresolvable ids (no project binding for them) are a safe no-op."""
    before = len(page._node_graph.nodes)
    _drop_pool_assets(page, ["ghost-id", "asset-img"])
    assert len(page._node_graph.nodes) == before + 1
    assert _source_nodes(page) == [("image_input", "/tmp/drop/a.png")]


def test_pool_drop_without_actions_binding_is_noop(qapp) -> None:
    """Pre-bind (no graph yet) the editor must not crash the drop path."""
    page = CompositePage()
    try:
        page._node_editor._actions = None
        before = len(page._node_graph.nodes)
        _drop_pool_assets(page, ["asset-img"])
        assert len(page._node_graph.nodes) == before
    finally:
        page.close()
