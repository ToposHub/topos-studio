"""M07 UI 入口 · 调色版本工具面板（清单/激活/锁标）。

接既有 SaveColorVersionCommand / ActivateColorVersionCommand /
SetStackLockCommand（F4.2 + M07.1 修复后的清单/缺失检查）。面板呈现
版本的插件清单与缺失状态（含 unverified），提供保存/激活/锁定操作
入口——命令经调用方栈提交（一次 Undo）。
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from src.features.edit.core.color_versions import versions_state
from src.shared.i18n.i18n import I18N


class ColorVersionToolsPanel(QDialog):
    """栈的命名版本管理：列表/保存/激活/锁定。"""

    def __init__(
        self,
        project: Any,
        stack_id: str,
        *,
        command_executor: Optional[Any] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._stack_id = stack_id
        self._executor = command_executor
        self.setWindowTitle(f"调色版本——{stack_id}")

        layout = QVBoxLayout(self)
        self._status = QLabel()
        layout.addWidget(self._status)
        self._list = QListWidget()
        # M07 版本比较：多选两版本 → 比较（diff 数据源 compare_versions）
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self._list, 1)

        btn_save = QPushButton(
            I18N.tr("version_tools.save_named", "保存当前为命名版本…"))
        btn_save.clicked.connect(self._on_save)
        layout.addWidget(btn_save)
        btn_activate = QPushButton(
            I18N.tr("version_tools.activate_selected", "激活选中版本"))
        btn_activate.clicked.connect(self._on_activate)
        layout.addWidget(btn_activate)
        btn_compare = QPushButton(
            I18N.tr("version_tools.compare_two", "比较选中两版本"))
        btn_compare.clicked.connect(self._on_compare)
        layout.addWidget(btn_compare)
        btn_lock = QPushButton(
            I18N.tr("version_tools.toggle_shared_lock", "切换共享锁"))
        btn_lock.clicked.connect(self._on_toggle_lock)
        layout.addWidget(btn_lock)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self._refresh()

    def _state(self):
        return versions_state(self._project, self._stack_id)

    def _refresh(self) -> None:
        state = self._state()
        self._list.clear()
        locked = bool(state.get("locked"))
        active = state.get("active")
        self._status.setText(
            f"共享锁：{'已锁定（非授权写入零提交）' if locked else '未锁定'}"
            f"｜活动版本：{active or '—'}")
        for name, payload in sorted(
                (state.get("versions") or {}).items()):
            plugins = payload.get("plugin_versions") or {}
            unverified = payload.get("plugin_check_unverified")
            tag = "●" if name == active else " "
            lock = " 🔒" if locked else ""
            plug_txt = (f"{len(plugins)} 插件" if plugins else "无插件")
            if unverified:
                plug_txt += "（清单未核对：注册表不可用）"
            item = QListWidgetItem(
                f"{tag} {name}{lock}｜{plug_txt}")
            item.setData(0x0100, name)
            self._list.addItem(item)

    def _selected_version(self) -> Optional[str]:
        item = self._list.currentItem()
        return item.data(0x0100) if item else None

    def _execute(self, command) -> None:
        """命令统一出口（N04）：executor 路径回读新工程，失败明示。

        executor 返回新工程对象（truthy）→ 面板回读刷新（不再持旧私有
        快照）；返回 False → 工程已切换/命令被拒，本面板不写不刷。
        直应用路径按 EditCommand 协议归一 ``(new, updated_cmd)`` 元组
        （ActivateColorVersion 等命令 apply 返回二元组）。
        """
        if self._executor is not None:
            result = self._executor(command)
            if not result:
                self._status.setText(
                    I18N.tr(
                        "version_tools.commit_rejected",
                        "提交被拒（工程已切换或命令失败）——本面板未写入"))
                return
            self._project = result  # 主工程回读
        else:
            # 无栈时直接应用到工程（测试路径）；EditCommand 协议 apply
            # 可返回 (new_project, updated_command) 二元组（Activate
            # 等）——归一取工程，更新后命令留待读取 missing_plugins。
            result = command.apply(self._project)
            if isinstance(result, tuple):
                updated_command, result = result[1], result[0]
                if updated_command is not None:
                    command = updated_command
            self._project = result
        self._last_command = command
        self._refresh()

    def _on_save(self) -> None:
        from src.features.edit.core.color_versions import (
            SaveColorVersionCommand,
        )
        name, ok = QInputDialog.getText(self, "保存版本", "版本名：")
        if not ok or not name.strip():
            return
        try:
            cmd = SaveColorVersionCommand.create(
                self._project, self._stack_id, name.strip())
        except ValueError as exc:
            QMessageBox.warning(self, "拒绝", str(exc))
            return
        if cmd.missing_plugins:
            confirm = QMessageBox.question(
                self, "插件缺失",
                f"版本清单缺 {len(cmd.missing_plugins)} 个插件："
                f"{', '.join(cmd.missing_plugins[:5])}…\n仍保存（缺失明示）？")
            if confirm != QMessageBox.StandardButton.Yes:
                return
        self._execute(cmd)

    def _on_activate(self) -> None:
        from src.features.edit.core.color_versions import (
            ActivateColorVersionCommand,
        )
        name = self._selected_version()
        if not name:
            return
        try:
            cmd = ActivateColorVersionCommand.create(
                self._stack_id, name)
        except ValueError as exc:
            QMessageBox.warning(self, "拒绝", str(exc))
            return
        # N04：激活与保存/锁定同走命令出口——不再绕过 executor 直改
        # 面板私有工程（旧路径主工程/命令栈完全不知情）。缺失提示从
        # 刷新后的版本 payload 读（保存期持久化的 missing_plugins_hint
        # ——executor/直应用两路一致，不依赖栈内更新后的命令对象）。
        self._execute(cmd)
        payload = (self._state().get("versions") or {}).get(name) or {}
        hint = payload.get("missing_plugins_hint") or ()
        if hint:
            QMessageBox.information(
                self, "激活完成",
                f"缺 {len(hint)} 个插件"
                f"（{', '.join(list(hint)[:5])}）——"
                "渲染将走缺失明示")

    def _on_toggle_lock(self) -> None:
        from src.features.edit.core.color_versions import SetStackLockCommand
        state = self._state()
        self._execute(SetStackLockCommand.create(
            self._project, self._stack_id,
            not bool(state.get("locked"))))

    def _on_compare(self) -> None:
        """M07：比较选中的两个版本（主调参数差异+插件清单差集）。"""
        selected = [item.data(0x0100)
                    for item in self._list.selectedItems()]
        if len(selected) != 2:
            self._status.setText("比较需恰好选中两个版本（当前 "
                                 f"{len(selected)} 个）")
            return
        from src.features.edit.core.color_versions import compare_versions
        try:
            report = compare_versions(
                self._project, self._stack_id, selected[0], selected[1])
        except ValueError as exc:
            self._status.setText(f"比较失败：{exc}")
            return
        QMessageBox.information(
            self, "版本比较", self._compare_text(report))

    @staticmethod
    def _compare_text(report: dict) -> str:
        """比较报告 → 可读文本（数据源 compare_versions；测试直测）。"""
        lines = [f"{report['a']} ↔ {report['b']}"]
        if report["same"]:
            lines.append("主调参数完全一致")
        else:
            lines.append(f"主调参数差异 {len(report['diffs'])} 项：")
            for key, (va, vb) in report["diffs"].items():
                lines.append(f"· {key}: {va!r} → {vb!r}")
        if report["plugin_only_a"] or report["plugin_only_b"]:
            lines.append(f"插件差集：仅 {report['a']} 有 "
                         f"{report['plugin_only_a']}；仅 {report['b']} 有 "
                         f"{report['plugin_only_b']}")
        return "\n".join(lines)

    @property
    def project(self):
        return self._project


__all__ = ["ColorVersionToolsPanel"]
