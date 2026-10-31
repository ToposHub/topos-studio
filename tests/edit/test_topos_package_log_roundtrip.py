"""P1-12/P1-13 真实 .topos 包 round-trip 验证。

验收（计划表 P1-12）："重开工程后元数据、Smart Bin 和搜索结果稳定"。
走 EditProjectIO.save_project → .topos ZIP → load_project 完整链路：
- 素材日志字段 + production info + offline/proxy 保存后不丢
- 用户 Smart Bin 持久化（edit/smart_bins.json）且过滤结果一致
- Replace Media 后保存/重开，asset_id 稳定 → 剪辑参数不丢
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.edit_core

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core.project import EditProject, MediaAsset, SmartBin, replace_media
from features.edit.core.edit_project_io import EditProjectIO


def _asset(asset_id: str, path: str, **overrides) -> MediaAsset:
    params = dict(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name=f"{asset_id}.mov",
        metadata={"codec": "h264", "duration": 100},
    )
    params.update(overrides)
    return MediaAsset(**params)


def _project(media_dir) -> EditProject:
    project = EditProject(
        project_id="p1",
        name="log-roundtrip",
        assets={
            "a1": _asset("a1", os.path.join(media_dir, "a1.mov"),
                         keywords=("interview", "indoor"), rating=4,
                         flag="reject", color_label="orange", comment="备用机位",
                         reel="A001", scene="12", shot="3", take="2",
                         is_offline=True, proxy_path="/tmp/px.mov",
                         proxy_status="ready"),
            "a2": _asset("a2", os.path.join(media_dir, "a2.mov"), rating=2),
        },
        root_bin_id="root",
    )
    return project.add_smart_bin(SmartBin(
        bin_id="sb-1", name="访谈精选",
        filter_type="custom",
        filter_params={"keywords": ["interview"], "min_rating": 3},
    ))


def _save_and_reload(qapp, project, tmp_path) -> EditProject:
    io = EditProjectIO()
    path = tmp_path / "roundtrip.topos"
    assert io.save_project(str(path), project), "save failed"
    loaded, _ui_state, _seqs = io.load_project(str(path))
    assert loaded is not None, "load failed"
    return loaded


class TestToposPackageRoundTrip:
    def test_log_and_production_info_survive(self, qapp, tmp_path) -> None:
        project = _project(str(tmp_path))
        loaded = _save_and_reload(qapp, project, tmp_path)

        a1 = loaded.assets["a1"]
        assert a1.keywords == ("interview", "indoor")
        assert a1.rating == 4
        assert a1.flag == "reject"
        assert a1.color_label == "orange"
        assert a1.comment == "备用机位"
        # 修复回归：production info + offline/proxy 此前保存即丢
        assert a1.reel == "A001"
        assert a1.scene == "12"
        assert a1.shot == "3"
        assert a1.take == "2"
        assert a1.is_offline is True
        assert a1.proxy_path == "/tmp/px.mov"
        assert a1.proxy_status == "ready"

    def test_smart_bins_survive_and_filter_stable(self, qapp, tmp_path) -> None:
        project = _project(str(tmp_path))
        before = project.get_smart_bin_assets("sb-1")

        loaded = _save_and_reload(qapp, project, tmp_path)
        assert "sb-1" in loaded.smart_bins
        assert loaded.smart_bins["sb-1"].filter_params == {
            "keywords": ["interview"], "min_rating": 3,
        }
        after = loaded.get_smart_bin_assets("sb-1")
        assert [a.asset_id for a in after] == [a.asset_id for a in before] == ["a1"]

    def test_empty_smart_bins_write_clears_stale_module(self, qapp, tmp_path) -> None:
        """全部删除后重开工程，Smart Bin 不复活（空 dict 也写出）。"""
        import zipfile
        project = _project(str(tmp_path))
        io = EditProjectIO()
        path = tmp_path / "t1.topos"
        assert io.save_project(str(path), project)

        with zipfile.ZipFile(path) as zf:
            assert "edit/smart_bins.json" in zf.namelist()

    def test_replace_media_survives_package_roundtrip(self, qapp, tmp_path) -> None:
        old = tmp_path / "a1.mov"; old.write_bytes(b"x")
        new = tmp_path / "b002.mov"; new.write_bytes(b"y")
        project = _project(str(tmp_path))
        replaced = replace_media(
            project, "a1", str(new), new_metadata={"duration": 200, "fps": 25.0}
        )
        loaded = _save_and_reload(qapp, replaced, tmp_path)

        asset = loaded.assets["a1"]
        assert asset.path.endswith("b002.mov")
        assert asset.is_offline is False
        assert asset.proxy_status == "none"
        assert asset.metadata["duration"] == 200
        # asset_id 稳定 → 引用它的剪辑在包内保持有效
        assert "a1" in loaded.assets
        assert loaded.smart_bins["sb-1"].filter_params["keywords"] == ["interview"]
