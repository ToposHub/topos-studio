"""C5：一次 Undo/Redo 覆盖新增素材与落轨（ImportDropMacroCommand）。

真实 EditProject + 真实宏命令机制；视图助手（_undo/_redo_imported_assets）
以既有 AST 手法从 TimelineView 提取真实实现驱动。子命令用最小记录桩
承载时间线语义（真实落轨语义由既有 drop/补偿回归与实机验收覆盖）。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

from src.features.edit.core.commands import (
    CommandStack,
    ImportDropMacroCommand,
    MacroCommand,
)
from src.features.edit.core.project import EditProject


def _load_view_method(name: str) -> Any:
    source = ast.parse(
        (ROOT / "src/features/edit/ui/timeline_view.py").read_text(encoding="utf-8"))
    cls = next(node for node in source.body
               if isinstance(node, ast.ClassDef) and node.name == "TimelineView")
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.Module(body=[method], type_ignores=[])
    namespace: dict[str, Any] = {"logger": logging.getLogger("test-c5")}
    exec(compile(module, f"timeline_view.{name}", "exec"), namespace)
    return namespace[name]


UNDO_ASSETS = _load_view_method("_undo_imported_assets")
REDO_ASSETS = _load_view_method("_redo_imported_assets")


def _clip_op(log: list, clip_id: str) -> Any:
    """最小时间线子命令：共享日志记录（宏 apply 会重建子命令副本，
    实例计数器会失联，断言只能看共享日志）。"""

    class _Op:
        """apply 返回新时间线对象（栈的 no-op 判定是同一性比较，
        原样透传会被当成无变更不入历史）；undo 回到前代对象。"""

        def apply(self, timeline: Any) -> Any:
            log.append(("apply", clip_id))
            return SimpleNamespace(all_tracks=[], previous=timeline)

        def undo(self, timeline: Any) -> Any:
            log.append(("undo", clip_id))
            return getattr(timeline, "previous", timeline)

    return _Op()


def _make_view(project: EditProject) -> SimpleNamespace:
    published: list[EditProject] = []
    return SimpleNamespace(
        _project=project,
        project_changed=SimpleNamespace(emit=published.append),
        published=published,
        _undo_imported_assets=None,  # 由测试按需绑定真实函数
        _redo_imported_assets=None,
    )


def _bind(view: SimpleNamespace) -> SimpleNamespace:
    import types

    view._undo_imported_assets = types.MethodType(UNDO_ASSETS, view)
    view._redo_imported_assets = types.MethodType(REDO_ASSETS, view)
    return view


def _timeline_without(removed_ids: set[str]) -> Any:
    """撤销后的时间线桩：仅保留未移除片段的引用。"""
    clips = [SimpleNamespace(asset_id=aid) for aid in ("kept-ref",)]
    track = SimpleNamespace(clips=clips)
    return SimpleNamespace(all_tracks=[track])


def test_single_undo_removes_clips_and_new_assets_keeps_reused():
    project = EditProject.create("c5")
    project = project.import_asset("/media/reused.mov", "video", {})
    project = project.import_asset("/media/new-a.mov", "video", {})
    reused = project.get_asset_by_path("/media/reused.mov")
    new_asset = project.get_asset_by_path("/media/new-a.mov")

    view = _bind(_make_view(project))
    view._project = project
    log: list = []
    macro = ImportDropMacroCommand.create(
        "Drop 2 external file(s)", (_clip_op(log, "clip-1"),),
        imported_assets=(new_asset,),
        on_undo_assets=view._undo_imported_assets,
        on_redo_assets=view._redo_imported_assets)

    stack = CommandStack()
    timeline = SimpleNamespace(all_tracks=[], name="tl")
    new_timeline = stack.execute(macro, timeline)
    assert ("apply", "clip-1") in log

    undone = stack.undo(new_timeline)
    assert ("undo", "clip-1") in log, "时间线侧回滚"
    assert view._project.get_asset_by_path("/media/new-a.mov") is None, "新导入素材随撤销移除"
    assert view._project.get_asset_by_path("/media/reused.mov") is not None, "复用素材不误删"

    redone = stack.redo(undone)
    assert log.count(("apply", "clip-1")) == 2
    restored = view._project.get_asset_by_path("/media/new-a.mov")
    assert restored is not None and restored.asset_id == new_asset.asset_id, \
        "重做按原 asset_id 复位（clip 引用不断链）"


def test_undo_keeps_asset_still_referenced_by_other_clip():
    project = EditProject.create("c5-keep")
    project = project.import_asset("/media/shared.mov", "video", {})
    asset = project.get_asset_by_path("/media/shared.mov")
    view = _bind(_make_view(project))
    view._project = project

    # 撤销宏后时间线上仍有其他片段引用该素材
    timeline = SimpleNamespace(all_tracks=[
        SimpleNamespace(clips=[SimpleNamespace(asset_id=asset.asset_id)])])

    UNDO_ASSETS(view, (asset,), timeline)  # noqa: SLF001 - AST 提取的直调
    assert view._project.get_asset_by_path("/media/shared.mov") is not None, \
        "仍被引用的素材跳过移除，不产生悬挂引用"
    assert view.published == [], "没有变更就不发布"


def test_undo_redo_are_idempotent_and_reuse_project():
    project = EditProject.create("c5-idem")
    project = project.import_asset("/media/one.mov", "video", {})
    asset = project.get_asset_by_path("/media/one.mov")
    view = _bind(_make_view(project))
    view._project = project

    empty_timeline = SimpleNamespace(all_tracks=[])
    UNDO_ASSETS(view, (asset,), empty_timeline)  # noqa: SLF001
    UNDO_ASSETS(view, (asset,), empty_timeline)  # noqa: SLF001 - 重复撤销幂等
    assert view._project.get_asset_by_path("/media/one.mov") is None

    REDO_ASSETS(view, (asset,))  # noqa: SLF001
    REDO_ASSETS(view, (asset,))  # noqa: SLF001 - 重复重做幂等
    assert view._project.get_asset_by_path("/media/one.mov").asset_id == asset.asset_id


def test_macro_subclass_survives_subcommand_self_update():
    """宏 apply 重建 updated_macro 时保子类（dataclasses.replace 路径）。"""

    log: list = []

    class _SelfUpdating:
        def __init__(self, clip_id: str) -> None:
            self.clip_id = clip_id

        def apply(self, timeline: Any) -> Any:
            log.append(("apply", self.clip_id))
            return timeline, _SelfUpdating(self.clip_id)

        def undo(self, timeline: Any) -> Any:
            log.append(("undo", self.clip_id))
            return timeline

    seen: list[tuple] = []
    project = EditProject.create("c5-sub")
    project = project.import_asset("/media/x.mov", "video", {})
    asset = project.get_asset_by_path("/media/x.mov")
    view = _bind(_make_view(project))
    view._project = project

    macro = ImportDropMacroCommand.create(
        "Drop 1", (_SelfUpdating("c"),),
        imported_assets=(asset,),
        on_undo_assets=lambda assets, tl: seen.append(("undo", len(assets))),
        on_redo_assets=lambda assets: seen.append(("redo", len(assets))))

    timeline = SimpleNamespace(all_tracks=[])
    result = macro.apply(timeline)
    assert isinstance(result, tuple), "子命令自我更新走 (timeline, macro) 通道"
    updated = result[1]
    assert isinstance(updated, ImportDropMacroCommand), "子类不退化为裸宏"
    assert updated.on_undo_assets is not None and updated.imported_assets == (asset,)

    updated.undo(result[0])
    # apply 的幂等 ensure 先触发 on_redo_assets，undo 再触发 on_undo_assets
    assert seen == [("redo", 1), ("undo", 1)], "重建后的宏仍携带素材回调"
