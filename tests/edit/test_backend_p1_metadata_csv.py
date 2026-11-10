"""P1-7 导入/导出元数据——CSV 交互验收。

对应《媒体池后端交接计划》P1 第 7 项：
- 匹配键优先稳定 asset_id，来源身份 path 兜底；
- 差异预览（plan 不写工程）+ 单步撤销命令；
- 字符编码（utf-8-sig/utf-8/gbk 解码）、重复匹配、空值清除/保持
  语义明确；失败（无法匹配/非法值）不做部分无提示写入。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.commands import (
    ImportMetadataCommand,
    ProjectCommandStack,
)
from src.features.edit.core.metadata_interchange import (
    decode_metadata_csv_bytes,
    export_metadata_csv,
    plan_metadata_import,
)
from src.features.edit.core.project import EditProject


def _project() -> tuple[EditProject, str, str]:
    project = EditProject.create("P1-7")
    project = project.import_asset("/m/a.mov", "video", {"fps": 25.0})
    project = project.import_asset("/m/b.mov", "video", {"fps": 25.0})
    ids = list(project.assets)
    return project, ids[0], ids[1]


HEADER = "asset_id,path,keywords,rating,flag,color_label,comment,reel,scene\n"


def test_export_roundtrip_columns_and_identity() -> None:
    project, a1, a2 = _project()
    project = project.update_asset_log_info(
        a1, keywords=("night", "ext"), rating=4, color_label="blue",
        comment="注意,逗号")
    project = project.update_asset_production_info(a1, reel="A001", scene=None,
                                                   shot=None, take=None)
    text = export_metadata_csv(project)
    lines = text.splitlines()
    assert lines[0].startswith("asset_id,path,display_name,keywords")
    row = lines[1]
    assert "night;ext" in row          # 关键词分号拼接（CSV 引号安全）
    assert '"注意,逗号"' in row        # 逗号正确加引号
    assert "A001" in row and "blue" in row
    # 只导出选中素材
    partial = export_metadata_csv(project, [a2])
    assert "/m/b.mov" in partial and "/m/a.mov" not in partial


def test_plan_matches_by_asset_id_then_path() -> None:
    project, a1, _ = _project()
    csv_text = (HEADER
                + f"{a1},/m/a.mov,interview,5,,,\n"
                + ",/m/b.mov,b-roll,3,,,\n")
    plan = plan_metadata_import(project, csv_text)
    assert plan.total_rows == 2
    assert not plan.unmatched_rows
    by_id = {c.asset_id: c for c in plan.asset_changes}
    assert by_id[a1].changed_fields
    assert any(f.field == "rating" and f.new_value == 5
               for f in by_id[a1].changed_fields)


def test_empty_cell_clears_missing_column_keeps() -> None:
    """空值语义：空单元格=清除；整列缺失=保持。"""
    project, a1, _ = _project()
    project = project.update_asset_log_info(
        a1, keywords=("old",), rating=3, comment="保留我")
    # keywords/rating 列存在且为空 → 清除；comment 列缺失 → 保持
    csv_text = f"asset_id,keywords,rating\n{a1},,\n"
    plan = plan_metadata_import(project, csv_text)
    fields = {f.field: f for f in
              next(c for c in plan.asset_changes
                   if c.asset_id == a1).changed_fields}
    assert fields["keywords"].new_value == ()
    assert fields["rating"].new_value == 0
    assert "comment" not in fields  # 列缺失不导入
    applied = plan and _apply(project, plan)
    assert applied.assets[a1].keywords == ()
    assert applied.assets[a1].rating == 0
    assert applied.assets[a1].comment == "保留我"


def _apply(project, plan):
    from src.features.edit.core.metadata_interchange import apply_metadata_import
    return apply_metadata_import(project, plan)


def test_invalid_values_and_unmatched_rows_are_warned_not_written() -> None:
    project, a1, _ = _project()
    csv_text = (HEADER
                + f"{a1},/m/a.mov,,9,blue-flag,,\n"     # rating 越界+flag 非法
                + "ghost-id,/m/ghost.mov,x,1,,,\n")     # 无法匹配
    plan = plan_metadata_import(project, csv_text)
    assert plan.unmatched_rows == (2,)
    assert any("rating" in w for w in plan.warnings)
    assert any("flag" in w for w in plan.warnings)
    applied = _apply(project, plan)
    assert applied.assets[a1].rating == 0
    assert applied.assets[a1].flag == "none"


def test_duplicate_rows_same_asset_merge_with_warning() -> None:
    project, a1, _ = _project()
    csv_text = HEADER + f"{a1},/m/a.mov,one,1,,,\n{a1},/m/a.mov,two,2,,,\n"
    plan = plan_metadata_import(project, csv_text)
    assert any("同一素材" in w for w in plan.warnings)
    applied = _apply(project, plan)
    assert applied.assets[a1].keywords == ("two",)  # 后行覆盖
    assert applied.assets[a1].rating == 2


def test_import_command_single_undo_restores_everything() -> None:
    project, a1, a2 = _project()
    project = project.update_asset_log_info(a1, keywords=("old",), rating=2)
    csv_text = (HEADER
                + f"{a1},/m/a.mov,new,5,reject,red,备注,A002,S42\n"
                + f"{a2},/m/b.mov,,3,,blue,,\n")
    plan = plan_metadata_import(project, csv_text)
    stack = ProjectCommandStack()
    project = stack.execute(ImportMetadataCommand(
        command_id="imp", label="Import Metadata", plan=plan), project)
    assert project.assets[a1].rating == 5
    assert project.assets[a1].flag == "reject"
    assert project.assets[a1].reel == "A002"
    assert project.assets[a2].color_label == "blue"
    project = stack.undo(project)
    assert project.assets[a1].keywords == ("old",)
    assert project.assets[a1].rating == 2
    assert project.assets[a1].flag == "none"
    assert project.assets[a1].reel == ""
    assert project.assets[a2].color_label == "none"


def test_decode_fallback_encodings() -> None:
    assert decode_metadata_csv_bytes("asset_id\näää\n".encode("utf-8-sig")) \
        .startswith("asset_id")
    assert decode_metadata_csv_bytes("备注\n".encode("gbk")).startswith("备注")
    with pytest.raises(ValueError, match="解码"):
        decode_metadata_csv_bytes(b"\xff\xfe\x00\xd8\x00\x00")
