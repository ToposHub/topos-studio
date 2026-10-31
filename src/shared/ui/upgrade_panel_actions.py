"""升级计划 M 各 UI 面板的统一挂接注册表（菜单接线件）。

页面控制器在菜单/工具栏构建时调用 :func:`panel_menu_actions` 获得
"动作 id → (标签, 构造器)" 的惰性注册表——构造器在触发时才解析
工程/时间线/节点上下文并实例化面板。注册表是**唯一**接线清单：
新增面板在此登记，菜单侧零散 if/else 不再扩散。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

PanelFactory = Callable[..., Any]


_REGISTRY: Dict[str, Tuple[str, PanelFactory]] = {}


def register_panel_action(
    action_id: str, label: str, factory: PanelFactory,
) -> None:
    """登记一个面板动作（重复 id 后写覆盖——菜单构建期一次性）。"""
    _REGISTRY[action_id] = (label, factory)


def panel_menu_actions() -> Dict[str, Tuple[str, PanelFactory]]:
    """当前注册表快照（菜单构建用）。"""
    return dict(_REGISTRY)


def open_panel(action_id: str, **context: Any) -> Any:
    """触发动作：以给定上下文实例化面板（返回实例——由调用方 show）。

    未登记 id / 上下文不足（工厂抛 TypeError/ValueError）→ 抛
    KeyError/原异常给调用方（菜单槽留日志），不静默吞。
    """
    if action_id not in _REGISTRY:
        raise KeyError(f"未登记的面板动作：{action_id}")
    label, factory = _REGISTRY[action_id]
    return factory(**context)


def register_default_actions() -> None:
    """登记升级计划 M 已交付的面板（幂等）。"""

    def _shot_match_panel(project, timeline, clip_ids, engine, **kw):
        from src.features.color.ui.shot_match_panel import (
            ShotMatchPanel, controller_measure_fn,
        )
        return ShotMatchPanel(
            project, str(getattr(timeline, "timeline_id", "")),
            clip_ids=clip_ids,
            # N10：frame_source 必须转传——无逐镜头真实帧源时面板
            # fail-closed（拒绝同图假测量），不再静默退化。
            measure_fn=controller_measure_fn(
                engine, kw.get("clip_frame_fn", lambda cid: None),
                frame_source=kw.get("frame_source")),
            reference_clip_id=kw.get("reference_clip_id", ""))

    def _version_tools_panel(project, stack_id, **kw):
        from src.features.edit.ui.m07_version_tools_panel import (
            ColorVersionToolsPanel,
        )
        # N04：命令出口必须转传——面板没有 executor 时会退化为改私有
        # 副本（主工程/命令栈不知情）。
        return ColorVersionToolsPanel(
            project, stack_id,
            command_executor=kw.get("command_executor"))

    def _filler_panel(detection, project, timeline, base_revision, **kw):
        from src.features.edit.ui.filler_removal_panel import (
            FillerRemovalPanel,
        )
        # N07：on_plan_ready 必须转传——面板生成 PREVIEW 计划后交真实
        # 审批/事务链；工厂吞参会让计划无消费者（accept 后即丢失）。
        return FillerRemovalPanel(
            detection, project=project, timeline=timeline,
            timeline_id=str(getattr(timeline, "timeline_id", "")),
            base_revision=base_revision,
            on_plan_ready=kw.get("on_plan_ready"))

    def _snapshot_compare_panel(project, **kw):
        from src.features.edit.ui.snapshot_compare_panel import (
            SnapshotComparePanel,
        )
        table = getattr(project, "project_snapshots", {}) or {}
        payloads = {name: entry.get("payload", {})
                    for name, entry in table.items()
                    if isinstance(entry, dict)}
        return SnapshotComparePanel(
            payloads, from_name=kw.get("from_name", ""),
            to_name=kw.get("to_name", ""))

    def _proxy_cleanup_panel(proxy_manager, **kw):
        from src.features.edit.ui.proxy_cleanup_panel import (
            ProxyCleanupPanel,
        )
        return ProxyCleanupPanel(proxy_manager)

    def _tracker_panel(tracker_node, retrack_fn=None, **kw):
        from src.features.color.ui.tracker_correction_panel import (
            TrackerCorrectionPanel,
        )
        # N13：retrack_fn/on_changed/undo_sink 全部转传——工厂吞参会让
        # 面板退化为"手工修正直改 node 不可撤销/无法重跟"。
        return TrackerCorrectionPanel(
            tracker_node, retrack_fn=retrack_fn,
            on_changed=kw.get("on_changed"),
            undo_sink=kw.get("undo_sink"))

    def _multi_version_qc_panel(lufs_report=None, subtitle_summary=None,
                                **kw):
        from src.features.deliver.ui.m21_multi_version_qc_panel import (
            MultiVersionQcPanel,
        )
        return MultiVersionQcPanel(
            lufs_report=lufs_report, subtitle_summary=subtitle_summary)

    def _bake_panel(graph, target_id, output_dir, frame_start, frame_end,
                    render_fn, **kw):
        """N05：Bake 对话框工厂——真实 render_fn/rewrite_fn 经宿主管辖。"""
        from src.features.composite.ui.bake_progress_dialog import (
            BakeProgressDialog,
        )
        return BakeProgressDialog(
            graph, str(target_id), str(output_dir),
            int(frame_start), int(frame_end), render_fn,
            rewrite_fn=kw.get("rewrite_fn"))

    defaults = {
        "color.shot_match": ("批量镜头匹配…", _shot_match_panel),
        "color.version_tools": ("调色版本工具…", _version_tools_panel),
        "edit.filler_removal": ("删除口头语…", _filler_panel),
        "project.snapshot_compare": ("比较工程快照…", _snapshot_compare_panel),
        "project.proxy_cleanup": ("代理与缓存管理…", _proxy_cleanup_panel),
        "composite.tracker_corrections": ("跟踪修正与重跟…", _tracker_panel),
        "deliver.multi_version_qc": ("多版本交付 QC…", _multi_version_qc_panel),
        "composite.bake": ("烘焙选中节点…", _bake_panel),
    }
    for action_id, (label, factory) in defaults.items():
        register_panel_action(action_id, label, factory)


register_default_actions()


__all__ = [
    "register_panel_action",
    "panel_menu_actions",
    "open_panel",
    "register_default_actions",
]
