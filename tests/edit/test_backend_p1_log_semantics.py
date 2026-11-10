"""P1-4 标记、旗标、Clip Color 完整业务语义——核验验收。

对应《媒体池后端交接计划》P1 第 4 项：
- Resolve 风格多旗标：核验结论 = 维持单旗标模型（none/reject）+ 9 色
  color_label 承担颜色维度；扩展多旗标需先声明 schema/迁移/命令，
  本轮不悄悄改模型（update_asset_log_info 域校验 fail-loud 锁定）；
- 来源级标记（asset_markers，P0-2）与时间线实例级标记
  （TimelineSequence.markers）是两个域，互不串用；
- Undo、批量、Smart Bin、搜索一致；旧日志不丢失。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.asset_source_range import (
    add_asset_marker,
    list_asset_markers,
    make_asset_marker,
)
from src.features.edit.core.commands import (
    BatchUpdateAssetLogCommand,
    ProjectCommandStack,
)
from src.features.edit.core.media_pool_query import (
    MediaPoolQuery,
    execute_media_pool_query,
)
from src.features.edit.core.project import (
    ASSET_COLOR_LABELS,
    ASSET_FLAG_VALUES,
    EditProject,
    SmartBin,
)
from src.features.edit.core.serialization import EditProjectSerializer
from src.features.edit.core.smart_bin_rules import RULES_KEY, SCHEMA_VERSION_KEY
from src.features.edit.core.timeline_v2 import (
    TimelineMarker,
    TimelineSequence,
)


def _project_two_assets() -> tuple[EditProject, str, str]:
    project = EditProject.create("P1-4")
    for name in ("alpha", "beta"):
        project = project.import_asset(
            f"/tmp/{name}.mov", "video", {"fps": 25.0})
    ids = [a.asset_id for a in project.assets.values()]
    return project, ids[0], ids[1]


def test_flag_and_color_domain_pinned_with_fail_loud() -> None:
    """单旗标+9色模型锁定：域外值 fail-loud（多旗标扩展需先声明 schema）。"""
    project, a1, _ = _project_two_assets()
    assert ASSET_FLAG_VALUES == ("none", "reject")
    assert len(ASSET_COLOR_LABELS) == 9
    with pytest.raises(ValueError, match="旗标"):
        project.update_asset_log_info(a1, flag="blue-flag")
    with pytest.raises(ValueError, match="颜色标签"):
        project.update_asset_log_info(a1, color_label="magenta")
    with pytest.raises(ValueError, match="评级"):
        project.update_asset_log_info(a1, rating=9)
    # 合法域内全通过
    project = project.update_asset_log_info(
        a1, flag="reject", color_label=ASSET_COLOR_LABELS[1], rating=4)


def test_batch_log_undo_restores_old_logs() -> None:
    """批量改日志：一次撤销全部复原，旧日志（关键词/评级/注释）不丢失。"""
    project, a1, a2 = _project_two_assets()
    project = project.update_asset_log_info(
        a1, keywords=("dailies",), rating=2, comment="原始注释")
    project = project.update_asset_log_info(a2, keywords=("night",))

    stack = ProjectCommandStack()
    project = stack.execute(BatchUpdateAssetLogCommand.create(
        (a1, a2), add_keywords=("archive",), rating=5, color_label="blue",
    ), project)
    assert "archive" in project.assets[a1].keywords
    assert project.assets[a1].rating == 5
    project = stack.undo(project)
    # 旧日志逐字段复原
    assert project.assets[a1].keywords == ("dailies",)
    assert project.assets[a1].rating == 2
    assert project.assets[a1].comment == "原始注释"
    assert project.assets[a1].color_label == "none"
    assert project.assets[a2].keywords == ("night",)


def test_smart_bin_and_search_agree_on_log_fields() -> None:
    """Smart Bin（规则树）与 All Fields 搜索对同一日志字段命中一致。"""
    project, a1, a2 = _project_two_assets()
    project = project.update_asset_log_info(
        a1, color_label="orange", keywords=("hero",))
    smart = SmartBin(
        bin_id="sb-color", name="Orange", filter_type="custom",
        filter_params={RULES_KEY: {"op": "and", "rules": [
            {"field": "color_label", "cmp": "==", "value": "orange"}]},
            SCHEMA_VERSION_KEY: 2})
    project = project._with_smart_bins({**project.smart_bins, "sb-color": smart})
    assert {a.asset_id for a in project.get_smart_bin_assets("sb-color")} == {a1}
    # All Fields 搜索同一颜色词 → 同一素材
    result = execute_media_pool_query(
        project, MediaPoolQuery(text="orange", field="all_fields",
                                scope="all_bins"))
    assert set(result.asset_ids) == {a1}
    # 关键词维度两端一致
    result_kw = execute_media_pool_query(
        project, MediaPoolQuery(text="hero", field="all_fields",
                                scope="all_bins"))
    assert set(result_kw.asset_ids) == {a1}


def test_source_markers_and_timeline_markers_are_separate_domains() -> None:
    """来源级标记（asset_markers）与时间线实例级标记互不串用、各自持久化。"""
    project, a1, _ = _project_two_assets()
    marker = make_asset_marker(10, name="素材级", color="#00ff00")
    project = add_asset_marker(project, a1, marker)

    from src.features.edit.core.timeline_v2 import Rational
    timeline = TimelineSequence(
        timeline_id="t1", name="T", fps=Rational(25, 1),
        markers=(TimelineMarker(marker_id="tm-1", frame=20, name="时间线级"),))

    # 两个域的标记互不出现
    assert [m["name"] for m in list_asset_markers(project.assets[a1])] == ["素材级"]
    assert [m.name for m in timeline.markers] == ["时间线级"]

    # 序列化往返：来源级标记随素材持久化（时间线标记走自己的通道）
    reopened = EditProjectSerializer.from_json(
        EditProjectSerializer.to_json(project))
    assert [m["marker_id"] for m in list_asset_markers(reopened.assets[a1])] == \
        [marker["marker_id"]]
    assert reopened.assets[a1].flag == "none"  # 未动日志不受标记操作影响
