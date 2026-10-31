"""UI contract tests for the Composite asset dependency panel."""

from __future__ import annotations

from src.features.composite.core.asset_dependency import (
    AssetDependencyReport,
    AssetReference,
    STATUS_MISSING,
    STATUS_RESOLVED,
)
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.ui.asset_dependency_panel import (
    AssetDependencyPanel,
)
from src.features.composite.ui.composite_page import CompositePage


def _report() -> AssetDependencyReport:
    return AssetDependencyReport(references=(
        AssetReference(
            node_id="media-1", node_type="video_frame",
            parameter="file_path", role="media", purpose="媒体源",
            stored_path="plate.mov", resolved_path="/tmp/plate.mov",
            status=STATUS_RESOLVED,
        ),
        AssetReference(
            node_id="lut-1", node_type="lut",
            parameter="lut_path", role="lut", purpose="3D LUT",
            stored_path="missing.cube", resolved_path=None,
            status=STATUS_MISSING,
        ),
    ))


def test_panel_renders_report_counts_and_paths(qapp) -> None:
    panel = AssetDependencyPanel()
    panel.update_report(_report())

    assert panel._table.rowCount() == 2
    assert panel._table.item(0, 0).text() == "media-1"
    assert panel._table.item(0, 3).text() == "已解析"
    assert panel._table.item(1, 4).text() == "missing.cube"
    assert "共 2 项" in panel._summary.text()
    assert "缺失 1" in panel._summary.text()


def test_panel_emits_refresh_relink_and_collect_requests(qapp) -> None:
    panel = AssetDependencyPanel()
    panel.update_report(_report())
    refreshes: list[bool] = []
    relinks: list[tuple[str, str]] = []
    collects: list[bool] = []
    search_root_requests: list[bool] = []
    panel.refresh_requested.connect(lambda: refreshes.append(True))
    panel.relink_requested.connect(lambda node, param: relinks.append((node, param)))
    panel.collect_requested.connect(lambda: collects.append(True))
    panel.search_root_requested.connect(lambda: search_root_requests.append(True))

    panel._refresh_button.click()
    panel._collect_button.click()
    panel._search_root_button.click()
    panel._table.cellWidget(1, 5).click()

    assert refreshes == [True]
    assert collects == [True]
    assert search_root_requests == [True]
    assert relinks == [("lut-1", "lut_path")]


def test_panel_disables_action_for_resolved_reference(qapp) -> None:
    panel = AssetDependencyPanel()
    panel.update_report(_report())

    assert not panel._table.cellWidget(0, 5).isEnabled()
    panel.set_operation_message("操作完成")
    assert panel._summary.text() == "操作完成"


def test_panel_displays_search_roots_with_full_path_tooltip(qapp) -> None:
    panel = AssetDependencyPanel()

    panel.set_search_roots(("/tmp/assets", "/tmp/library"))

    assert panel._search_roots_label.text() == "搜索目录：assets、library"
    assert panel._search_roots_label.toolTip() == "/tmp/assets\n/tmp/library"

    panel.set_search_roots(())
    assert panel._search_roots_label.text() == "搜索目录：未设置"
    assert panel._search_roots_label.toolTip() == ""


def test_page_persists_asset_search_roots_in_viewer_state() -> None:
    graph = NodeGraph()
    published: list[bool] = []
    page = type("PageStub", (), {})()
    page._asset_search_roots = ()
    page._node_graph = graph
    page._asset_dependency_panel = None
    page._publish_asset_graph_change = lambda: published.append(True)

    CompositePage.set_asset_search_roots(page, ("~/assets",), persist=True)

    assert page._asset_search_roots[0].endswith("/assets")
    assert graph._viewer_state["asset_search_roots"] == list(
        page._asset_search_roots)
    assert published == [True]
