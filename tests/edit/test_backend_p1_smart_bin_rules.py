"""P1-1 Smart Bins 完整规则编辑器——规则 schema 与查询执行验收。

对应《媒体池后端交接计划》P1 第 1 项：
- 嵌套 AND/OR、比较操作符、日期区间、关键词、颜色/旗标、媒体属性；
- 保存、编辑、删除和重开一致；未知 filter_params 字段保留；
- 不同媒体类型不会错误匹配（图片不命中 fps 规则、缺失值不产生假阳性）。
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.commands import (
    ProjectCommandStack,
    UpdateSmartBinRulesCommand,
)
from src.features.edit.core.project import EditProject, MediaAsset, SmartBin
from src.features.edit.core.serialization import EditProjectSerializer
from src.features.edit.core.smart_bin_rules import (
    COMPARATORS,
    RULES_KEY,
    SCHEMA_VERSION_KEY,
    SmartBinRuleError,
    matches_rule_tree,
    smart_bin_rule_capabilities,
    validate_rule_tree,
)


def _video(asset_id: str, name: str, *, fps=25.0, width=1920, height=1080,
           rating=0, keywords=(), codec="h264", color_label="none",
           flag="none", created_at=None) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path=f"/m/{name}.mov", media_type="video",
        display_name=name,
        metadata={"fps": fps, "width": width, "height": height,
                  "codec": codec, "total_frames": 100,
                  "duration_seconds": 4.0},
        rating=rating, keywords=keywords, color_label=color_label, flag=flag,
        created_at=created_at if created_at is not None else time.time(),
    )


def _image(asset_id: str, name: str) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path=f"/m/{name}.png", media_type="image",
        display_name=name, metadata={"width": 4000, "height": 3000},
        created_at=time.time(),
    )


from dataclasses import replace as _dc_replace


def _add_assets(project: EditProject, *assets: MediaAsset) -> EditProject:
    return _dc_replace(project, assets={
        **project.assets, **{a.asset_id: a for a in assets}})


# ================================================================
# 校验与能力清单
# ================================================================

def test_capability_list_covers_plan_domains() -> None:
    """能力清单给前端：媒体属性/技术字段/日志字段/时间戳齐全。"""
    caps = {cap.field for cap in smart_bin_rule_capabilities()}
    assert {"media_type", "fps", "width", "codec", "duration_seconds",
            "keywords", "rating", "color_label", "flag", "comment",
            "created_at"} <= caps
    # 每个能力声明的操作符都在合法域内
    for cap in smart_bin_rule_capabilities():
        assert set(cap.ops) <= set(COMPARATORS)
        assert cap.value_type in ("number", "string", "enum", "boolean",
                                  "timestamp", "string_list")


@pytest.mark.parametrize("bad,path_hint", [
    ({"op": "and", "rules": []}, "非空"),
    ({"op": "xor", "rules": [{"field": "fps", "cmp": "==", "value": 25}]}, "组合操作符"),
    ({"field": "nope", "cmp": "==", "value": 1}, "未知规则字段"),
    ({"field": "keywords", "cmp": ">=", "value": 1}, "不支持操作符"),
    ({"field": "created_at", "cmp": "between", "value": [1]}, "between"),
    ({"field": "fps", "cmp": "exists", "value": "yes"}, "exists"),
    ({"field": "fps", "cmp": "==", "value": None}, "需要 value"),
])
def test_validate_rejects_malformed_trees(bad, path_hint) -> None:
    with pytest.raises(SmartBinRuleError, match=path_hint):
        validate_rule_tree(bad)


# ================================================================
# 求值语义
# ================================================================

def test_nested_and_or_with_comparators() -> None:
    tree = {
        "op": "and",
        "rules": [
            {"field": "media_type", "cmp": "==", "value": "video"},
            {"field": "fps", "cmp": ">=", "value": 24.0},
            {
                "op": "or",
                "rules": [
                    {"field": "rating", "cmp": ">=", "value": 4},
                    {"field": "keywords", "cmp": "contains", "value": "hot"},
                ],
            },
        ],
    }
    hi = _video("a1", "hi", rating=5)
    hot = _video("a2", "hot", keywords=("dailies", "hot"))
    low = _video("a3", "low")
    slow = _video("a4", "slow", fps=12.0)
    img = _image("a5", "img")
    assert matches_rule_tree(hi, tree)
    assert matches_rule_tree(hot, tree)
    assert not matches_rule_tree(low, tree)
    assert not matches_rule_tree(slow, tree)
    # 图片无 fps（不适用字段按缺失）→ 不匹配
    assert not matches_rule_tree(img, tree)


def test_date_range_and_missing_values() -> None:
    now = time.time()
    fresh = _video("a1", "fresh", created_at=now - 60)
    old = _video("a2", "old", created_at=now - 90 * 86400)
    window = {"field": "created_at", "cmp": "between",
              "value": [now - 3600, now]}
    assert matches_rule_tree(fresh, {"op": "and", "rules": [window]})
    assert not matches_rule_tree(old, {"op": "and", "rules": [window]})
    # 缺失值：无 fps 的素材不命中任何 fps 比较（含 !=，不产生假阳性）
    unknown_fps = MediaAsset(
        asset_id="a3", path="/m/x.mov", media_type="video",
        display_name="x", metadata={"width": 1920, "height": 1080},
        created_at=now)
    for cmp_op in ("==", "!=", ">", "<"):
        rule = {"field": "fps", "cmp": cmp_op, "value": 25.0}
        assert not matches_rule_tree(unknown_fps, {"op": "and", "rules": [rule]})
    # exists 显式判断有无
    assert matches_rule_tree(unknown_fps, {"op": "and", "rules": [
        {"field": "fps", "cmp": "exists", "value": False}]})
    assert not matches_rule_tree(_video("a4", "y"), {"op": "and", "rules": [
        {"field": "fps", "cmp": "exists", "value": False}]})


def test_string_in_and_between_and_flag_color() -> None:
    tree = {
        "op": "or",
        "rules": [
            {"field": "codec", "cmp": "in", "value": ["prores", "dNxhD"]},
            {"field": "color_label", "cmp": "==", "value": "blue"},
            {"field": "flag", "cmp": "==", "value": "reject"},
        ],
    }
    assert matches_rule_tree(_video("a1", "p", codec="prores"), tree)
    assert matches_rule_tree(_video("a2", "d", codec="dnxhd"), tree)  # 大小写不敏感
    assert matches_rule_tree(_video("a3", "b", color_label="blue"), tree)
    assert matches_rule_tree(_video("a4", "r", flag="reject"), tree)
    assert not matches_rule_tree(_video("a5", "x", codec="h264"), tree)
    # between 闭区间
    band = {"field": "width", "cmp": "between", "value": [1920, 3840]}
    assert matches_rule_tree(_video("a6", "in", width=1920),
                             {"op": "and", "rules": [band]})
    assert not matches_rule_tree(_video("a7", "out", width=1000),
                                 {"op": "and", "rules": [band]})


# ================================================================
# SmartBin 集成 + 命令 + 序列化
# ================================================================

def test_smart_bin_rule_tree_query_via_project() -> None:
    project = EditProject.create("P1")
    for index, (name, rating) in enumerate(
            [("good", 5), ("mid", 3), ("bad", 1)]):
        asset = _video(f"a{index}", name, rating=rating)
        project = _add_assets(project, asset)
    tree = {"op": "and", "rules": [
        {"field": "rating", "cmp": ">=", "value": 3}]}
    smart = SmartBin(bin_id="sb", name="Rated", filter_type="custom",
                     filter_params={RULES_KEY: tree, SCHEMA_VERSION_KEY: 2})
    project = project._with_smart_bins({**project.smart_bins, "sb": smart})
    matched = project.get_smart_bin_assets("sb")
    assert {a.display_name for a in matched} == {"good", "mid"}
    # 旧平铺 filter_params 继续可用（零迁移共存）
    legacy = SmartBin(bin_id="sb2", name="Legacy", filter_type="custom",
                      filter_params={"min_rating": 3})
    project = project._with_smart_bins({**project.smart_bins, "sb2": legacy})
    assert {a.display_name for a in project.get_smart_bin_assets("sb2")} == \
        {"good", "mid"}


def test_edit_rules_command_undo_and_reopen_consistency() -> None:
    project = EditProject.create("P1")
    asset = _video("a0", "clip", rating=5)
    project = _add_assets(project, asset)
    v1 = SmartBin(bin_id="sb", name="V1", filter_type="custom",
                  filter_params={RULES_KEY: {"op": "and", "rules": [
                      {"field": "rating", "cmp": ">=", "value": 4}]},
                      SCHEMA_VERSION_KEY: 2})
    project = project._with_smart_bins({**project.smart_bins, "sb": v1})

    stack = ProjectCommandStack()
    v2 = SmartBin(bin_id="sb", name="V2", filter_type="custom",
                  filter_params={RULES_KEY: {"op": "and", "rules": [
                      {"field": "rating", "cmp": ">=", "value": 1}]},
                      SCHEMA_VERSION_KEY: 2, "custom_future_key": "keep"})
    project = stack.execute(UpdateSmartBinRulesCommand.create(v2), project)
    assert project.smart_bins["sb"].name == "V2"
    assert {a.asset_id for a in project.get_smart_bin_assets("sb")} == {"a0"}
    # 非法规则树 fail-loud，不入栈
    broken = SmartBin(bin_id="sb", name="broken", filter_type="custom",
                      filter_params={RULES_KEY: {"field": "nope", "cmp": "==",
                                                 "value": 1}})
    with pytest.raises(SmartBinRuleError):
        stack.execute(UpdateSmartBinRulesCommand.create(broken), project)
    project = stack.undo(project)
    assert project.smart_bins["sb"].name == "V1"
    assert {a.asset_id for a in project.get_smart_bin_assets("sb")} == {"a0"}

    # 重开工程一致 + 未知 filter_params 键保留
    reopened = EditProjectSerializer.from_json(
        EditProjectSerializer.to_json(project))
    restored = reopened.smart_bins["sb"]
    assert restored.name == "V1"
    assert restored.filter_params[RULES_KEY]["rules"][0]["value"] == 4
    reopened_v2 = EditProjectSerializer.from_json(EditProjectSerializer.to_json(
        stack.redo(project)))
    assert reopened_v2.smart_bins["sb"].filter_params.get(
        "custom_future_key") == "keep"
