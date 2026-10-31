from __future__ import annotations

from PyQt6.QtGui import QPixmap

from src.features.edit.core.project import MediaAsset
from src.features.edit.ui.media_pool_panel import AssetListModel, GridItem


def test_comp_clip_reuses_source_thumbnail_and_refreshes_card(qapp) -> None:
    source = MediaAsset(
        asset_id="source-1",
        path="/tmp/source.mov",
        media_type="video",
        display_name="source.mov",
        metadata={},
    )
    comp = MediaAsset(
        asset_id="comp-1",
        path="topos://comp_clip/comp-1",
        media_type="comp_clip",
        display_name="Comp Clip 1",
        metadata={
            "original_asset_id": source.asset_id,
            "original_asset_path": source.path,
        },
    )

    model = AssetListModel()
    model.set_items([
        GridItem("asset", source.asset_id, source),
        GridItem("asset", comp.asset_id, comp),
    ])

    preview = QPixmap(32, 18)
    preview.fill()
    model.set_thumbnail(source.path, preview)

    cached = model.thumbnail_for_path(comp.path)
    assert cached is not None
    assert not cached.isNull()
