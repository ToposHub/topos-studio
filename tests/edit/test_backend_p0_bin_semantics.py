"""P0-5 素材箱移动语义与事务——验收测试。

产品约定（对齐 Resolve 呈现，存储保持引用式、旧工程零迁移）：
- 普通拖拽 = 移动（exclusive）：目标箱成为唯一普通箱归属；
- Alt 拖拽 = 拷贝引用（多箱共存）；
- 移回 Master = unfile（移出全部普通箱）；
- 嵌套箱循环防护、删除箱与 Undo 全部一致；
- 旧工程（多箱引用）可打开且 undo 逐箱复原。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.commands import (
    DeleteBinCommand,
    MoveAssetBetweenBinsCommand,
    MoveAssetToBinCommand,
    MoveBinCommand,
    ProjectCommandStack,
    UnfileAssetCommand,
)
from src.features.edit.core.project import EditProject
from src.features.edit.core.serialization import EditProjectSerializer


def _project_two_bins_one_asset():
    project = EditProject.create("P0 bins")
    project = project.import_asset(
        "/tmp/p0-bin.mov", "video", {"fps": 25.0, "total_frames": 10})
    project = project.create_bin("A", project.root_bin_id)
    project = project.create_bin("B", project.root_bin_id)
    bin_a = next(b.bin_id for b in project.bins.values() if b.name == "A")
    bin_b = next(b.bin_id for b in project.bins.values() if b.name == "B")
    asset_id = project.get_asset_by_path("/tmp/p0-bin.mov").asset_id
    return project, asset_id, bin_a, bin_b


def _bins_containing(project, asset_id):
    """素材归属的**用户箱**集合（root 是隐式容器，不计入）。"""
    return {
        b.bin_id for b in project.bins.values()
        if b.bin_id != project.root_bin_id and asset_id in b.asset_ids
    }


# ================================================================
# 模型：exclusive move / unfile / membership 原语
# ================================================================

def test_move_exclusive_removes_other_bins_keeps_target() -> None:
    project, asset_id, bin_a, bin_b = _project_two_bins_one_asset()
    project = project.move_asset_to_bin(asset_id, bin_a)
    project = project.move_asset_to_bin(asset_id, bin_b)  # 引用式：双箱共存
    assert _bins_containing(project, asset_id) == {bin_a, bin_b}
    project = project.move_asset_exclusive(asset_id, bin_a)
    assert _bins_containing(project, asset_id) == {bin_a}


def test_unfile_returns_to_master() -> None:
    project, asset_id, bin_a, bin_b = _project_two_bins_one_asset()
    project = project.move_asset_to_bin(asset_id, bin_a)
    project = project.move_asset_to_bin(asset_id, bin_b)
    project = project.unfile_asset(asset_id)
    assert _bins_containing(project, asset_id) == set()
    assert asset_id in project.get_unfiled_asset_ids()


def test_membership_primitive_rejects_unknown_asset() -> None:
    project = EditProject.create("empty")
    with pytest.raises(ValueError):
        project.with_asset_membership("nope", {})


# ================================================================
# 命令：移动/移回的 undo 恢复完整跨箱归属
# ================================================================

def test_move_command_undo_restores_multi_bin_membership() -> None:
    """旧工程多箱引用：移动后 undo 逐箱复原（不只回一个箱）。"""
    project, asset_id, bin_a, bin_b = _project_two_bins_one_asset()
    project = project.create_bin("C", project.root_bin_id)
    bin_c = next(b.bin_id for b in project.bins.values() if b.name == "C")
    # 历史工程：素材同时在 A、B
    project = project.move_asset_to_bin(asset_id, bin_a)
    project = project.move_asset_to_bin(asset_id, bin_b)

    stack = ProjectCommandStack()
    project = stack.execute(
        MoveAssetBetweenBinsCommand.create(asset_id, bin_c), project)
    assert _bins_containing(project, asset_id) == {bin_c}
    project = stack.undo(project)
    assert _bins_containing(project, asset_id) == {bin_a, bin_b}
    project = stack.redo(project)
    assert _bins_containing(project, asset_id) == {bin_c}


def test_unfile_command_undo_restores_membership() -> None:
    project, asset_id, bin_a, bin_b = _project_two_bins_one_asset()
    project = project.move_asset_to_bin(asset_id, bin_a)
    project = project.move_asset_to_bin(asset_id, bin_b)
    stack = ProjectCommandStack()
    project = stack.execute(UnfileAssetCommand.create(asset_id), project)
    assert _bins_containing(project, asset_id) == set()
    project = stack.undo(project)
    assert _bins_containing(project, asset_id) == {bin_a, bin_b}


def test_reference_copy_command_keeps_multi_bin() -> None:
    """Alt 拷贝引用（MoveAssetToBinCommand）在移动语义下仍可用。"""
    project, asset_id, bin_a, bin_b = _project_two_bins_one_asset()
    project = project.move_asset_exclusive(asset_id, bin_a)
    stack = ProjectCommandStack()
    project = stack.execute(
        MoveAssetToBinCommand.create(asset_id, bin_b), project)
    assert _bins_containing(project, asset_id) == {bin_a, bin_b}


# ================================================================
# UI 路由：普通拖拽=移动 / Alt=拷贝 / root=移回 Master
# ================================================================

@pytest.fixture
def panel(qapp):
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel
    from src.shared.media.thumbnail_provider import ThumbnailProvider
    provider = ThumbnailProvider.instance()
    provider_get = provider.get_thumbnail
    provider.get_thumbnail = lambda *a, **k: None  # type: ignore[method-assign]
    panel = MediaPoolPanel()
    panel._offline_refresh_timer.stop()
    yield panel
    provider.get_thumbnail = provider_get  # type: ignore[method-assign]
    panel._refresh_timer.stop()
    panel._offline_refresh_timer.stop()
    panel.close()
    panel.deleteLater()
    qapp.processEvents()


def test_panel_plain_drop_is_exclusive_move(panel, qapp) -> None:
    project, asset_id, bin_a, bin_b = _project_two_bins_one_asset()
    project = project.move_asset_to_bin(asset_id, bin_a)
    project = project.move_asset_to_bin(asset_id, bin_b)
    panel.set_project(project)
    qapp.processEvents()
    assert panel._handle_drop_into_bin(bin_a, [asset_id], [], copy=False) is True
    got = panel._project
    assert _bins_containing(got, asset_id) == {bin_a}
    # 一次撤销恢复双箱
    got = panel._command_stack.undo(got)
    assert _bins_containing(got, asset_id) == {bin_a, bin_b}


def test_panel_alt_drop_copies_reference(panel, qapp) -> None:
    project, asset_id, bin_a, bin_b = _project_two_bins_one_asset()
    project = project.move_asset_exclusive(asset_id, bin_a)
    panel.set_project(project)
    qapp.processEvents()
    assert panel._handle_drop_into_bin(bin_b, [asset_id], [], copy=True) is True
    got = panel._project
    assert _bins_containing(got, asset_id) == {bin_a, bin_b}


def test_panel_root_drop_unfiles_from_all_bins(panel, qapp) -> None:
    project, asset_id, bin_a, bin_b = _project_two_bins_one_asset()
    project = project.move_asset_to_bin(asset_id, bin_a)
    project = project.move_asset_to_bin(asset_id, bin_b)
    panel.set_project(project)
    qapp.processEvents()
    assert panel._handle_drop_into_bin(
        project.root_bin_id, [asset_id], []) is True
    got = panel._project
    assert _bins_containing(got, asset_id) == set()
    assert asset_id in got.get_unfiled_asset_ids()
    # root 隐式容器引用保持不变
    assert asset_id in got.bins[got.root_bin_id].asset_ids
    got = panel._command_stack.undo(got)
    assert _bins_containing(got, asset_id) == {bin_a, bin_b}


def test_panel_drop_idempotent_when_exclusively_in_target(panel, qapp) -> None:
    project, asset_id, bin_a, _bin_b = _project_two_bins_one_asset()
    project = project.move_asset_exclusive(asset_id, bin_a)
    panel.set_project(project)
    qapp.processEvents()
    assert panel._handle_drop_into_bin(bin_a, [asset_id], []) is False


# ================================================================
# 嵌套箱循环防护 / 删除箱 / 旧工程兼容
# ================================================================

def test_nested_bin_cycle_protection() -> None:
    project, _asset, bin_a, bin_b = _project_two_bins_one_asset()
    # B 挂到 A 下，再把 A 挂到 B（形成环）必须被拒绝
    project = project.reparent_bin(bin_b, bin_a)
    with pytest.raises(ValueError):
        project.reparent_bin(bin_a, bin_b)
    stack = ProjectCommandStack()
    with pytest.raises(ValueError):
        stack.execute(MoveBinCommand.create(bin_a, bin_b), project)
    # 失败不留半状态
    got = stack.undo(project) if stack.can_undo() else project
    assert got.bins[bin_a].parent_id == project.root_bin_id


def test_delete_bin_with_descendants_undo(panel, qapp) -> None:
    """删除箱：子箱上移一级（AUD-17102）、素材回未归类、undo 全恢复。"""
    project, asset_id, bin_a, bin_b = _project_two_bins_one_asset()
    project = project.reparent_bin(bin_b, bin_a)
    project = project.move_asset_to_bin(asset_id, bin_a)
    stack = ProjectCommandStack()
    project = stack.execute(DeleteBinCommand.create(bin_a), project)
    assert bin_a not in project.bins
    # 子箱上移到被删箱的父级（root），不悬挂
    assert bin_b in project.bins
    assert project.bins[bin_b].parent_id == project.root_bin_id
    # 素材本体保留（回到未归类）
    assert asset_id in project.assets
    assert asset_id in project.get_unfiled_asset_ids()
    project = stack.undo(project)
    assert bin_a in project.bins and bin_b in project.bins
    assert project.bins[bin_b].parent_id == bin_a
    assert asset_id in project.bins[bin_a].asset_ids


def test_legacy_multi_bin_project_opens_and_roundtrips() -> None:
    """旧工程（多箱引用，无 P0-5 字段）序列化往返不丢归属。"""
    project, asset_id, bin_a, bin_b = _project_two_bins_one_asset()
    project = project.move_asset_to_bin(asset_id, bin_a)
    project = project.move_asset_to_bin(asset_id, bin_b)
    reopened = EditProjectSerializer.from_json(
        EditProjectSerializer.to_json(project))
    assert _bins_containing(reopened, asset_id) == {bin_a, bin_b}
