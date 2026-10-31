"""P8.6 AiWorkflowModule —— AI Workflow FeatureModule 装配（plan §10.7 / §7.1）。

> AI Workflow 作为高级入口，通过同一 :class:`HarnessRuntime` 执行（plan §10.7）。

提供 :class:`AiWorkflowModule`（实现 :class:`FeatureModule` Protocol）：

- ``feature_id = "ai_workflow"``
- ``is_available()``：检查 PyQt6 UI 组件是否可导入；feature flag 在模块外部检查。
- ``register_i18n()``：加载 ``locales/`` 翻译。
- ``register_panels()``：当前不注册 dock；工作流界面由 AI Assistant 入口按需打开为弹窗。

红线（plan §0 / §10.7）：

- ``ai_workflow`` 在 ``src/shared/feature_flags.py`` 中默认开启，可通过 feature flag 覆盖。
- 界面按需创建和显示，不作为默认工作区页面或常驻 dock。
- 不直接写 timeline；变更经 workflow/action 服务处理并由宿主审批。
"""
from __future__ import annotations

import logging
import os
from typing import Any, List

logger = logging.getLogger(__name__)


class AiWorkflowModule:
    """AI Workflow feature module（plan §10.7）。

    FeatureModule Protocol 实现。dock 的实际创建在
    ``dock_layout_controller.setup_docks`` 里完成（参考 AiEditAssistantPanel 装配模式）；
    本 module 负责 i18n + panel descriptor 注册。
    """

    feature_id = "ai_workflow"

    def __init__(self) -> None:
        self._available: bool = False
        try:
            # 检查 PyQt6 + 核心 UI 是否可 import
            from src.features.ai_workflow.ui.workflow_panel import WorkflowPanel  # noqa: F401
            from src.features.ai_workflow.ui.workflow_canvas_adapter import (  # noqa: F401
                WorkflowCanvasAdapter,
            )
            self._available = True
        except ImportError as e:
            logger.warning(f"AI Workflow module unavailable: {e}")
            self._available = False

    # ---------------- FeatureModule Protocol ---------------- #

    def is_available(self) -> bool:
        return self._available

    def register_pages(self, context: Any) -> List:
        """AI Workflow 不注册顶级 page（作为 Edit 页 dock）。

        Returns:
            空列表（无顶级 page）。
        """
        return []

    def register_actions(self, context: Any) -> None:
        """注册菜单 action（本批无；Run/Cancel 留 P9）。"""
        return None

    def register_save_handlers(self, context: Any) -> None:
        """把 workflow 存储接进工程保存/加载（plan §13：工作流存进项目文件）。

        复刻 teaching_animation 模式：经 ``main_window.edit_project_io.set_callbacks``
        注册 payload 取/还原回调。弹窗与 panel 由 dock_layout_controller 懒建，
        回调侧用 getattr 容错（未建时返回空存储）。
        """
        main_window = getattr(context, "get_main_window", lambda: None)()
        if main_window is None:
            return
        edit_project_io = getattr(main_window, "edit_project_io", None)
        if edit_project_io is None:
            return
        get_payload = getattr(main_window, "_get_ai_workflow_payload", None)
        on_loaded = getattr(main_window, "_restore_ai_workflow_payload", None)
        try:
            edit_project_io.set_callbacks(
                get_ai_workflow_payload=get_payload,
                on_ai_workflow_payload_loaded=on_loaded,
            )
        except TypeError:
            logger.debug("edit_project_io lacks ai_workflow callback slots", exc_info=True)

    def register_i18n(self) -> None:
        """加载 AI Workflow 模块翻译。"""
        from src.shared.i18n.i18n import I18N
        locales_dir = os.path.join(os.path.dirname(__file__), "locales")
        I18N.load_module_translations(locales_dir)

    def register_panels(self, panel_registry) -> None:
        """AI Workflow 改为弹窗触发（右键 AI Assistant 面板 → 显示工作流）。

        不再注册 dock panel descriptor：Window > Panels 菜单不出现 AI Workflow 项。
        dock 创建已从 dock_layout_controller 移除，改为懒建弹窗（见
        ``_show_ai_workflow_dialog``）。
        """
        return None

    def on_page_enter(self, page_id: str, context: Any) -> None:
        """进入页面钩子（本批空实现）。"""
        return None

    def on_page_leave(self, page_id: str, context: Any) -> None:
        """离开页面钩子（本批空实现）。"""
        return None
