"""
P1-12 素材日志能力测试：数据模型 + 持久化 round-trip。

覆盖：
- MediaAsset 日志字段（keywords/rating/flag/color_label/comment）的默认值与校验
- with_log_fields 部分更新；with_path 等既有工厂方法保留日志字段
  （回归 dataclasses.replace 重构）
- EditProject.update_asset_log_info 部分更新与错误路径
- 序列化 round-trip：日志字段 + production info + offline/proxy 不丢失
  （回归 serialization._asset_to_dict 字段缺口 bug）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import (
    ASSET_COLOR_LABELS,
    ASSET_FLAG_VALUES,
    EditProject,
    MediaAsset,
)
from src.features.edit.core.serialization import EditProjectSerializer


def _asset(**overrides) -> MediaAsset:
    params = dict(
        asset_id="a-1",
        path="/tmp/clip.mov",
        media_type="video",
        display_name="clip.mov",
        metadata={},
    )
    params.update(overrides)
    return MediaAsset(**params)


def _project(*assets: MediaAsset) -> EditProject:
    return EditProject(
        project_id="p1",
        name="proj",
        assets={a.asset_id: a for a in assets},
        root_bin_id="root",
    )


# ================================================================
# MediaAsset 日志字段
# ================================================================

class TestMediaAssetLogFields:
    def test_defaults(self) -> None:
        asset = _asset()
        assert asset.keywords == ()
        assert asset.rating == 0
        assert asset.flag == "none"
        assert asset.color_label == "none"
        assert asset.comment == ""

    def test_full_construction(self) -> None:
        asset = _asset(
            keywords=("采访", "室内"),
            rating=4,
            flag="reject",
            color_label="red",
            comment="对白不清",
        )
        assert asset.keywords == ("采访", "室内")
        assert asset.rating == 4
        assert asset.flag == "reject"
        assert asset.color_label == "red"
        assert asset.comment == "对白不清"

    def test_invalid_rating_rejected(self) -> None:
        with pytest.raises(ValueError):
            _asset(rating=6)
        with pytest.raises(ValueError):
            _asset(rating=-1)

    def test_invalid_flag_rejected(self) -> None:
        with pytest.raises(ValueError):
            _asset(flag="favorite")

    def test_invalid_color_label_rejected(self) -> None:
        with pytest.raises(ValueError):
            _asset(color_label="magenta")

    def test_keywords_must_be_tuple_of_str(self) -> None:
        with pytest.raises(ValueError):
            _asset(keywords=["list", "not", "tuple"])
        with pytest.raises(ValueError):
            _asset(keywords=(1, 2))

    def test_color_and_flag_value_tables(self) -> None:
        # 合法值表与文档一致
        assert "reject" in ASSET_FLAG_VALUES
        assert "none" in ASSET_COLOR_LABELS
        assert len(ASSET_COLOR_LABELS) == 9


class TestWithLogFields:
    def test_partial_update_keeps_other_fields(self) -> None:
        asset = _asset(
            keywords=("a",), rating=3, flag="reject", color_label="blue", comment="c"
        )
        updated = asset.with_log_fields(rating=5)
        assert updated.rating == 5
        assert updated.keywords == ("a",)
        assert updated.flag == "reject"
        assert updated.color_label == "blue"
        assert updated.comment == "c"

    def test_keywords_normalized_to_tuple(self) -> None:
        updated = _asset().with_log_fields(keywords=("x", "y"))
        assert isinstance(updated.keywords, tuple)
        assert updated.keywords == ("x", "y")

    def test_none_arguments_keep_original(self) -> None:
        asset = _asset(keywords=("k",), rating=2, comment="note")
        updated = asset.with_log_fields()
        assert updated == asset or updated.keywords == asset.keywords
        assert updated.rating == 2
        assert updated.comment == "note"

    def test_with_path_preserves_log_fields(self) -> None:
        """回归：重定位等既有工厂方法不得丢日志字段（replace 重构保障）。"""
        asset = _asset(
            keywords=("k1",), rating=5, flag="reject", color_label="green", comment="c"
        )
        moved = asset.with_path("/tmp/new.mov")
        assert moved.path == "/tmp/new.mov"
        assert moved.keywords == ("k1",)
        assert moved.rating == 5
        assert moved.flag == "reject"
        assert moved.color_label == "green"
        assert moved.comment == "c"

    def test_with_production_info_preserves_log_fields(self) -> None:
        asset = _asset(keywords=("k",), rating=3)
        updated = asset.with_production_info(reel="R1")
        assert updated.reel == "R1"
        assert updated.keywords == ("k",)
        assert updated.rating == 3


# ================================================================
# EditProject.update_asset_log_info
# ================================================================

class TestUpdateAssetLogInfo:
    def test_partial_update(self) -> None:
        asset = _asset(keywords=("old",), rating=1)
        project = _project(asset)
        project2 = project.update_asset_log_info("a-1", keywords=("new", "dailies"), rating=4)
        updated = project2.assets["a-1"]
        assert updated.keywords == ("new", "dailies")
        assert updated.rating == 4
        # 原工程不可变
        assert project.assets["a-1"].keywords == ("old",)
        assert project.assets["a-1"].rating == 1

    def test_unknown_asset_raises(self) -> None:
        project = _project(_asset())
        with pytest.raises(ValueError):
            project.update_asset_log_info("ghost", rating=5)

    def test_update_does_not_drop_other_project_state(self) -> None:
        asset = _asset()
        project = _project(asset)
        project2 = project.update_asset_log_info("a-1", flag="reject")
        assert project2.project_id == project.project_id
        assert project2.bins == project.bins
        assert set(project2.assets) == set(project.assets)


# ================================================================
# 持久化 round-trip（字段缺口回归）
# ================================================================

class TestSerializationRoundTrip:
    def _rich_asset(self) -> MediaAsset:
        return _asset(
            keywords=("航拍", "日落"),
            rating=5,
            flag="reject",
            color_label="orange",
            comment="备份机位",
            reel="A001",
            scene="12",
            shot="3",
            take="2",
            is_offline=True,
            proxy_path="/tmp/proxy.mov",
            proxy_status="ready",
        )

    def test_asset_dict_round_trip_preserves_every_field(self) -> None:
        asset = self._rich_asset()
        data = EditProjectSerializer._asset_to_dict(asset)
        restored = EditProjectSerializer._dict_to_asset(data)
        # 关键持久化字段逐一比对（metadata dict 等不在比对范围）
        for field_name in (
            "asset_id", "path", "media_type", "display_name", "thumbnail_key",
            "proxy_path", "proxy_status", "is_offline", "created_at",
            "reel", "scene", "shot", "take",
            "keywords", "rating", "flag", "color_label", "comment",
        ):
            assert getattr(restored, field_name) == getattr(asset, field_name), field_name

    def test_asset_to_dict_writes_legacy_missing_fields(self) -> None:
        """回归：reel/is_offline/proxy_path 曾经不被写出，保存即丢失。"""
        asset = self._rich_asset()
        data = EditProjectSerializer._asset_to_dict(asset)
        assert data["reel"] == "A001"
        assert data["is_offline"] is True
        assert data["proxy_path"] == "/tmp/proxy.mov"

    def test_project_json_round_trip_preserves_log_fields(self) -> None:
        project = _project(self._rich_asset())
        json_str = EditProjectSerializer.to_json(project)
        restored = EditProjectSerializer.from_json(json_str)
        asset = restored.assets["a-1"]
        assert asset.keywords == ("航拍", "日落")
        assert asset.rating == 5
        assert asset.flag == "reject"
        assert asset.color_label == "orange"
        assert asset.comment == "备份机位"
        assert asset.reel == "A001"
        assert asset.is_offline is True

    def test_old_schema_without_log_fields_still_loads(self) -> None:
        """不含日志字段的旧工程数据可加载，取默认值。"""
        data = EditProjectSerializer._asset_to_dict(_asset())
        legacy = {k: v for k, v in data.items() if k not in (
            "keywords", "rating", "flag", "color_label", "comment",
            "reel", "scene", "shot", "take", "is_offline", "proxy_path",
        )}
        restored = EditProjectSerializer._dict_to_asset(legacy)
        assert restored.keywords == ()
        assert restored.rating == 0
        assert restored.flag == "none"
        assert restored.color_label == "none"
        assert restored.comment == ""
