"""C5: Read/Write version binning (path_versioning util + wiring hooks)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.utils import path_versioning as pv
from src.features.composite.utils.path_versioning import (
    bump_version, parse_version)

pytestmark = pytest.mark.composite_core


class TestParseVersion:
    """Version recognition table (plan §C5: last token wins)."""

    def test_basic_v_padding(self):
        assert pv.parse_version("/shots/bg_plate_v003.exr") == 3

    def test_no_version(self):
        assert pv.parse_version("/shots/plate.exr") is None
        assert pv.parse_version("") is None

    def test_variants(self):
        assert pv.parse_version("V2_render.png") == 2
        assert pv.parse_version("plate.ver_03.exr") == 3
        assert pv.parse_version("plate.version-4.exr") == 4
        assert pv.parse_version("plate v007.exr") == 7

    def test_last_token_wins(self):
        # directory version + file version → file wins
        assert pv.parse_version("/shots/shot010/v003/plate_v002.exr") == 2
        assert pv.parse_version("v001/plate_v002.exr") == 2

    def test_directory_only_version(self):
        assert pv.parse_version("/shots/shot_v010/plate.exr") == 10

    def test_padding_preserved_on_bump(self):
        assert pv.bump_version("plate_v003.exr", 1) == "plate_v004.exr"
        assert pv.bump_version("plate_v003.exr", -2) == "plate_v001.exr"
        # crossing the padding boundary keeps the prefix (3 → 10 has no pad)
        assert pv.bump_version("plate_v009.exr", 1) == "plate_v010.exr"
        # never below zero
        assert pv.bump_version("plate_v001.exr", -5) == "plate_v000.exr"

    def test_bump_without_version_returns_none(self):
        assert pv.bump_version("plate.exr", 1) is None


class TestDiskListing:
    """Existing-version discovery (glob same directory, same pattern)."""

    @pytest.fixture()
    def versioned_tree(self, tmp_path):
        root = tmp_path / "shots"
        root.mkdir()
        for v in ("v001", "v002", "v004"):
            d = root / v
            d.mkdir()
            (d / "plate_v001.exr") if False else None
        # file-level versioning tree: plate_v001..v003.exr (002 missing)
        for n in ("001", "003"):
            (root / f"plate_v{n}.exr").write_bytes(b"x")
        return root

    def test_list_versions_sorted(self, versioned_tree):
        path = str(versioned_tree / "plate_v001.exr")
        versions = pv.list_versions(path)
        assert [os.path.basename(v) for v in versions] == [
            "plate_v001.exr", "plate_v003.exr"]

    def test_stepped_version_only_existing(self, versioned_tree):
        path = str(versioned_tree / "plate_v001.exr")
        # v002 does not exist → step up lands on v003
        assert pv.stepped_version(path, 1) == str(versioned_tree / "plate_v003.exr")
        # v000 does not exist → no step down
        assert pv.stepped_version(path, -1) is None
        # from the top there is nothing newer
        top = str(versioned_tree / "plate_v003.exr")
        assert pv.stepped_version(top, 1) is None
        assert pv.stepped_version(top, -1) == str(
            versioned_tree / "plate_v001.exr")

    def test_latest_version(self, versioned_tree):
        path = str(versioned_tree / "plate_v001.exr")
        assert pv.latest_version(path) == str(
            versioned_tree / "plate_v003.exr")
        # no version token → untouched
        assert pv.latest_version(str(versioned_tree / "plate.exr")) is None

    def test_directory_version_listing(self, tmp_path):
        root = tmp_path
        for v in ("v001", "v002"):
            d = root / v
            d.mkdir()
            (d / "plate.exr").write_bytes(b"x")
        path = str(root / "v001" / "plate.exr")
        latest = pv.latest_version(path)
        assert latest is not None and latest.endswith(os.path.join("v002", "plate.exr"))


class TestWidgetHookContracts:
    """The editor exposes the C5 hooks the page connects to."""

    def test_signals_and_shortcuts_registered(self):
        from src.features.composite.ui.node_editor_widget import (
            NodeEditorWidget as NodeGraphEditor,
        )
        assert hasattr(NodeGraphEditor, "node_version_step_requested")
        assert hasattr(NodeGraphEditor, "set_all_reads_latest_requested")

        import inspect
        src = inspect.getsource(NodeGraphEditor._setup_shortcuts)
        assert '"node_editor/version_up"' in src
        assert '"node_editor/version_down"' in src
        # bracket shortcuts bound
        assert 'Version Up (Read/Write)' in src

    def test_step_ignores_non_versionable_nodes(self, qapp):
        """Only image_input / file_output nodes emit the step request."""
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.ui.node_editor_widget import NodeEditorWidget as NodeGraphEditor

        editor = NodeGraphEditor()
        g = NodeGraph()
        editor.build_from_graph(g)
        from src.features.composite.nodes.blur_node import BlurNode
        g.add_node(BlurNode(node_id="b1"))
        editor._selected_node_id = "b1"

        got = []
        editor.node_version_step_requested.connect(
            lambda nid, d: got.append((nid, d)))
        editor._version_step_selected(1)
        assert got == []  # blur is not versionable
        editor.close()


class TestWordBoundary:
    """A version token needs a non-alphanumeric left boundary — ordinary
    words like 'preview_v2' bin, 'av1_master'/'rev2_final' must not."""

    def test_embedded_word_v_is_not_a_version(self):
        assert parse_version("video_av1_master.mov") is None
        assert parse_version("rev2_final.exr") is None

    def test_directory_token_still_binds(self):
        assert parse_version("shot010/v003/plate_v002.exr") == 2

    def test_bump_ignores_non_version_words(self):
        assert bump_version("av1_master.mov", 1) is None
        assert bump_version("preview_v2_final.exr", 1) == "preview_v3_final.exr"
