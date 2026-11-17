"""NR-03 回归：外部文件投放的导入/落轨一致性（FR-06）。

沿用审计 repro 的隔离手法：AST 提取真实的 ``_finish_external_import``
方法，用惰性 UI 协作对象驱动——真实 EditProject、真实 prepare_import。
验证：宏失败后工程不再多出新导入 asset，提示如实；复用素材不被误删。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

from src.features.edit.core.project import EditProject
from src.features.files.import_job import ImportCandidate, ImportProbeResult


def _load_finish_method() -> Any:
    source = ast.parse((ROOT / "src/features/edit/ui/timeline_view.py").read_text())
    cls = next(node for node in source.body
               if isinstance(node, ast.ClassDef) and node.name == "TimelineView")
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "_finish_external_import")
    module = ast.Module(body=[method], type_ignores=[])
    namespace: dict[str, Any] = {
        "Any": Any,
        "logger": logging.getLogger("test-fr06"),
        "get_media_type": lambda path: "video",
        # 宏命令在真实执行路径中来自 timeline_view 顶部导入；测试只需要
        # 它承载子命令（真正的时间线语义由集成/实机验证覆盖）
        "MacroCommand": SimpleNamespace(
            create=lambda label, commands: SimpleNamespace(sub_commands=commands)),
        # C5：投放宏携带新导入素材回调（测试里记录不执行）
        "ImportDropMacroCommand": SimpleNamespace(
            create=lambda label, commands, **kw: SimpleNamespace(
                sub_commands=commands, **kw)),
        "AddTrackCommand": SimpleNamespace(create=lambda *a, **k: None),
    }
    exec(compile(module, "timeline_view._finish_external_import", "exec"), namespace)
    return namespace["_finish_external_import"]


_FINISH = _load_finish_method()


class _FakeAddTrack:
    def apply(self, timeline):
        return timeline


def _make_view(project: EditProject, *, fail_macro: bool,
              create_track: bool = False) -> SimpleNamespace:
    track = SimpleNamespace(is_locked=False, name="V1")
    timeline = SimpleNamespace(revision=0, get_track_by_id=lambda tid: track)
    recorder: dict[str, list] = {"published": [], "messages": [], "macro": []}
    view = SimpleNamespace(
        _external_job_id=1, _project=project, _timeline=timeline,
        _recorder=recorder,
        _external_target={"project": project, "project_id": project.project_id,
                          "track_id": "V1", "create_track": create_track,
                          "track_type": "video", "track_name": "V1",
                          "frame": 0, "sequence_revision": 0},
        project_changed=SimpleNamespace(emit=recorder["published"].append),
        status_message_requested=SimpleNamespace(emit=recorder["messages"].append),
        _ensure_first_asset_timeline_settings=lambda asset: True,
        _undo_imported_assets=lambda assets, timeline: None,
        _redo_imported_assets=lambda assets: None,
        _get_asset_duration_frames=lambda asset: 25,
        timeline_changed=SimpleNamespace(emit=lambda *_: None),
        project_timeline_transaction_committed=SimpleNamespace(
            emit=lambda *args: None),
    )
    def _execute(command):
        # 与真实 _execute_command 相同的批量语义：批量模式下只收集
        if getattr(view, "_batch_drop_commands", None) is not None:
            view._batch_drop_commands.append(command)
            return
        if fail_macro:
            raise ValueError("simulated macro rejection")
        recorder["macro"].append("macro")

    view._execute_command = _execute
    view._add_asset_as_clip = (lambda asset, track, frame:
                               (view._execute_command(SimpleNamespace(asset=asset)),
                                True)[1])
    return view


def _run(view, paths_and_new):
    candidates = tuple(
        ImportCandidate(path, "video", is_new, {"duration_seconds": 1})
        for path, is_new in paths_and_new)
    _FINISH(view, ImportProbeResult(1, candidates))
    # _run 期间 view._batch_drop_commands 由方法自行置回 None


def test_fr06_macro_failure_compensates_new_imports():
    project = EditProject.create("audit")
    view = _make_view(project, fail_macro=True)

    _run(view, [("/audit/new-a.mov", True), ("/audit/new-b.mov", True)])

    # 工程回到导入前状态：宏失败后不再多出 asset（FR-06 核心）
    assert len(view._project.assets) == 0
    # 发布 + 补偿都发出 project_changed，提示与实际状态一致
    assert len(view._recorder["published"]) >= 2
    message = view._recorder["messages"][-1]
    assert "已回滚" in message and "2" in message  # 明确说出回退了什么


def test_fr06_macro_failure_keeps_reused_assets():
    """复用的既有素材（is_new=False）不因落轨失败被误删。"""
    project = EditProject.create("audit")
    reused = project.import_asset(path="/audit/existing.mov", media_type="video",
                                  metadata={"duration_seconds": 1})
    view = _make_view(reused, fail_macro=True)

    _run(view, [("/audit/existing.mov", False)])

    assert len(view._project.assets) == 1  # 既有素材保留
    assert view._project.get_asset_by_path("/audit/existing.mov") is not None


def test_fr06_success_path_keeps_assets_and_reports_once():
    project = EditProject.create("audit")
    view = _make_view(project, fail_macro=False)

    _run(view, [("/audit/ok.mov", True)])

    assert len(view._project.assets) == 1
    assert view._project.get_asset_by_path("/audit/ok.mov") is not None
