"""N24 · 升级面板共用宿主：生命周期 + 提交 adapter + 过期守卫。

审计缺口（docs/upgrade_audit_followup_2026-10-25.md §2 N24）：菜单经
``upgrade_panel_actions`` 注册表打开的面板此前只 ``show()``——无 parent、
无引用保持、无关闭清理、accepted 结果无消费者；多次点击产生多个并存
实例；换工程后旧对话框仍可写入。本模块把"宿主职责"从各菜单回调里
收拢为单一入口：

- **主窗口拥有面板**：parent 传主窗口（宿主 owner 是 QWidget 时），
  WA_DeleteOnClose；宿主持强引用直到 ``finished``；
- **单实例策略**：同 action_id 已开 → 激活既有窗口，不再叠加；
- **过期守卫**：打开时记录 ``project_id``；``reconcile`` 在工程切换后
  关闭不属于当前工程的窗口；``guard`` 在提交前二次校验（关闭窗口的
  迟到 accepted 也拒绝）——"换工程拒绝旧对话框写入"由此强制；
- **提交 adapter**：``project_command_executor`` 复刻 EditController
  正典提交链（media_pool_panel 命令栈 → 新工程回写主窗口 → dirty →
  set_project → project_changed），供面板 ``command_executor`` 注入
  （N04 版本工具等）；``timeline_command_executor`` 走
  TimelineView.execute_command（时间线域命令）；
- **构造失败明示**：面板创建/显示异常经 owner 的用户可见提示通道，
  不再只留日志（headless 退化为日志与旧口径一致）。

结果处理约定（避免一次重写所有领域）：面板保留各自 QDialog 语义，
宿主只连 ``accepted`` → ``result_handler(panel)``；处理器内**必须**先
``host.guard(panel)``，再取面板结果属性提交。取消/拒绝零变更由面板
自身不提交保证（现有面板 accepted 才产出结果属性）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: 面板结果处理器：accepted 后调用，返回 False 表示提交被拒（过期等）
PanelResultHandler = Callable[[Any], bool]


@dataclass
class _PanelRecord:
    action_id: str
    panel: Any
    project_id: Optional[str]
    result_handler: Optional[PanelResultHandler] = None
    closed: bool = False


@dataclass
class HostReport:
    """open/reconcile 的机器可读结果（测试与诊断用）。"""
    action_id: str
    opened: bool = False
    activated_existing: bool = False
    failed_reason: Optional[str] = None
    stale_closed: list[str] = field(default_factory=list)


def _project_identity(project: Any) -> Optional[str]:
    """工程稳定身份（project_id）；缺工程上下文的面板记 None。"""
    return getattr(project, "project_id", None)


class UpgradePanelHost:
    """升级面板共用宿主（N24）。

    owner 约定：MenuToolbarMixin 宿主（MainWindow 实例）——提供
    ``_upgrade_panel_context()`` / ``_upgrade_panel_unavailable()`` /
    ``media_pool_panel`` / ``_current_edit_project`` 等尽力而为属性。
    """

    def __init__(self, owner: Any) -> None:
        self._owner = owner
        self._records: dict[str, _PanelRecord] = {}

    # ── 生命周期 ──

    @property
    def open_action_ids(self) -> list[str]:
        return [aid for aid, rec in self._records.items() if not rec.closed]

    def active_panel(self, action_id: str) -> Any:
        rec = self._records.get(action_id)
        if rec is None or rec.closed:
            return None
        return rec.panel

    def open_panel(self, action_id: str, context: dict,
                   *, result_handler: Optional[PanelResultHandler] = None
                   ) -> HostReport:
        """经注册表打开面板（单实例+引用保持+结果接线+失败明示）。"""
        report = HostReport(action_id=action_id)
        # 每次打开先对账：工程切换后遗留的旧窗口就地关闭（迟到写入由
        # guard 兜底——没有主动关闭路径也能拒绝写入）。
        report.stale_closed = self.reconcile(self._current_project())
        existing = self.active_panel(action_id)
        if existing is not None:
            self._activate(existing)
            report.activated_existing = True
            return report
        from src.shared.ui.upgrade_panel_actions import open_panel
        try:
            panel = open_panel(action_id, **context)
        except Exception as exc:  # noqa: BLE001 — 构造失败必须明示不吞
            reason = f"{type(exc).__name__}: {exc}"
            report.failed_reason = reason
            self._notify_failure(action_id, reason)
            return report
        if panel is None:
            report.failed_reason = "factory returned None"
            self._notify_failure(action_id, report.failed_reason)
            return report
        parent = self._owner if _is_widget(self._owner) else None
        try:
            from PyQt6.QtCore import Qt
            from PyQt6.QtWidgets import QDialog, QWidget
            if not isinstance(panel, QWidget):
                report.failed_reason = "not a QWidget"
                self._notify_failure(action_id, report.failed_reason)
                return report
            # 对话框保持顶层窗口（setParent 会把它变成内嵌子部件：
            # 父窗口隐藏时不可见、close 不走 finished 路径）；"主窗口
            # 拥有"由宿主强引用承担。非对话框面板才挂 parent。
            if not isinstance(panel, QDialog) and parent is not None:
                panel.setParent(parent)
            panel.setAttribute(
                Qt.WidgetAttribute.WA_DeleteOnClose)
            panel.destroyed.connect(
                lambda _obj, aid=action_id: self._on_finished(aid))
            if isinstance(panel, QDialog):
                panel.finished.connect(
                    lambda _r, aid=action_id: self._on_finished(aid))
                if result_handler is not None:
                    panel.accepted.connect(
                        lambda aid=action_id: self._on_accepted(aid))
            panel.show()
        except Exception as exc:  # noqa: BLE001 — show 失败明示
            reason = f"{type(exc).__name__}: {exc}"
            report.failed_reason = reason
            self._notify_failure(action_id, reason)
            return report
        self._records[action_id] = _PanelRecord(
            action_id=action_id, panel=panel,
            project_id=_project_identity(context.get("project")),
            result_handler=result_handler)
        report.opened = True
        return report

    def _on_finished(self, action_id: str) -> None:
        # 解释器/GC 拆解期 destroyed 可能晚于宿主自身回收（循环引用
        # 先清 __dict__）——此时无事可清理，静默即可。
        records = getattr(self, "_records", None)
        if not records:
            return
        rec = records.get(action_id)
        if rec is not None:
            rec.closed = True

    def _on_accepted(self, action_id: str) -> None:
        rec = self._records.get(action_id)
        if rec is None or rec.closed or rec.result_handler is None:
            return
        try:
            ok = bool(rec.result_handler(rec.panel))
        except Exception:  # noqa: BLE001 — 处理器异常不崩宿主
            logger.exception("panel result handler %s failed", action_id)
            ok = False
        if not ok:
            self._notify_failure(
                action_id, "提交被拒绝（工程已切换或提交失败），结果未写入")

    # ── 过期守卫 ──

    def guard(self, panel: Any) -> bool:
        """提交前守卫：面板记录的工程必须仍是当前工程。"""
        current = self._current_project()
        rec = self._record_for_panel(panel)
        if rec is None:
            return False
        if rec.project_id is None:
            return True  # 非工程域面板（QC 呈现等）不受此门
        return rec.project_id == _project_identity(current)

    def reconcile(self, current_project: Any) -> list[str]:
        """工程切换后关闭不属于当前工程的窗口（迟到写入由 guard 兜底）。"""
        current_id = _project_identity(current_project)
        closed: list[str] = []
        for aid, rec in list(self._records.items()):
            if rec.closed or rec.project_id is None:
                continue
            if rec.project_id != current_id:
                closed.append(aid)
                rec.closed = True
                try:
                    rec.panel.close()
                except Exception:  # noqa: BLE001 — 关闭失败只记录
                    logger.debug("stale panel %s close failed", aid)
        return closed

    def _record_for_panel(self, panel: Any) -> Optional[_PanelRecord]:
        for rec in self._records.values():
            if rec.panel is panel:
                return rec
        return None

    def _current_project(self) -> Any:
        owner = self._owner
        return getattr(owner, "_current_edit_project", None) \
            or getattr(owner, "current_edit_project", None)

    # ── 提交 adapter ──

    def project_command_executor(
        self, *, on_error: Optional[Callable[[str], None]] = None
    ) -> Callable[[Any], Any]:
        """工程级命令提交器（复刻 EditController 正典链，N04 消费）。

        返回 executor(command) -> **新工程对象**（成功；truthy，面板可
        直接回读刷新）或 False（失败：栈/工程不可用、命令被拒——零半
        提交，命令栈 execute 自身原子）。
        """
        owner = self._owner

        def execute(command: Any) -> Any:
            panel = getattr(owner, "media_pool_panel", None)
            project = getattr(owner, "_current_edit_project", None)
            if panel is None or project is None:
                self._report_error(on_error, "工程命令栈不可用（未加载工程）")
                return False
            try:
                new_project = panel.get_command_stack().execute(
                    command, project)
            except Exception as exc:  # noqa: BLE001 — 命令拒绝/失败明示
                self._report_error(on_error, f"命令执行失败：{exc}")
                return False
            owner._current_edit_project = new_project
            owner.edit_project_dirty = True
            try:
                panel.set_project(new_project)
                panel.project_changed.emit(new_project)
            except Exception:  # noqa: BLE001 — 广播失败不影响已提交事实
                logger.exception("project_changed broadcast failed")
            return new_project

        return execute

    def timeline_command_executor(
        self, timeline_id: str, *,
        on_error: Optional[Callable[[str], None]] = None
    ) -> Callable[[Any], bool]:
        """时间线域命令提交器（TimelineView.execute_command 单入口）。"""
        owner = self._owner

        def execute(command: Any) -> bool:
            view = getattr(owner, "timeline_view", None) \
                or getattr(owner, "edit_page", None)
            executor = getattr(view, "execute_command", None) if view else None
            if executor is None:
                self._report_error(on_error, "时间线命令入口不可用")
                return False
            try:
                executor(command)
            except Exception as exc:  # noqa: BLE001
                self._report_error(on_error, f"时间线命令失败：{exc}")
                return False
            return True

        return execute

    # ── 内部 ──

    @staticmethod
    def _report_error(on_error: Optional[Callable[[str], None]],
                      message: str) -> None:
        if on_error is not None:
            try:
                on_error(message)
            except Exception:  # noqa: BLE001
                logger.debug("on_error callback failed")
        logger.warning("panel commit rejected: %s", message)

    def _notify_failure(self, action_id: str, reason: str) -> None:
        notify = getattr(self._owner, "_upgrade_panel_unavailable", None)
        message = f"面板 {action_id} 不可用：{reason}"
        if callable(notify):
            try:
                notify(message)
                return
            except Exception:  # noqa: BLE001
                logger.debug("notify failed for %s", action_id)
        logger.warning(message)

    @staticmethod
    def _activate(panel: Any) -> None:
        try:
            show = getattr(panel, "showNormal", None)
            if callable(show):
                show()
            raise_ = getattr(panel, "raise_", None)
            if callable(raise_):
                raise_()
            activate = getattr(panel, "activateWindow", None)
            if callable(activate):
                activate()
        except Exception:  # noqa: BLE001 — 激活尽力而为
            logger.debug("activate existing panel failed")


def _is_widget(obj: Any) -> bool:
    try:
        from PyQt6.QtWidgets import QWidget
        return isinstance(obj, QWidget)
    except Exception:  # noqa: BLE001 — 无 Qt 环境（纯逻辑测试）
        return False


__all__ = ["UpgradePanelHost", "HostReport", "PanelResultHandler"]
