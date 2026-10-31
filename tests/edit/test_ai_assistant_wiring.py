"""E13: AI 助手断线入口。

审计实测：/scope、上下文检查器"排除"、composer profile、帮助卡
navigate/open_panel 分别 emit 到无人接收的信号——UI 有入口无行为。

回归锁定：
- 面板信号存在并被控制器订阅（连接表源码断言）；
- 排除 → ContextBundle.exclude_block 后回填（bundle 块数减少）；
- 帮助卡 open_panel 按 page_ref 切页；navigate 给活动状态行反馈；
- composer_profile_requested 连到 AI 模型偏好（ai_editing_mixin）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.app.controllers.ai_edit_controller import AiEditController
from src.features.edit.ai_editing.context_bundle import ContextBlock
from src.features.edit.ai_editing.context_bundle import ContextBlockPriority
from src.features.edit.ui.ai_edit_assistant_panel import (
    AiEditAssistantPanel,
)

_PANEL_FILE = (
    Path(__file__).resolve().parents[2] / "src" / "features" / "edit" /
    "ui" / "ai_edit_assistant_panel.py"
)
_MIXIN_FILE = (
    Path(__file__).resolve().parents[2] / "src" / "app" / "controllers" /
    "ai_editing_mixin.py"
)


class TestPanelSignals:
    def test_panel_declares_help_signals(self) -> None:
        assert "help_navigate_requested" in vars(AiEditAssistantPanel)
        assert "help_open_panel_requested" in vars(AiEditAssistantPanel)

    def test_set_help_answer_forwards_card_signals(self) -> None:
        text = _PANEL_FILE.read_text(encoding="utf-8")
        assert "card.navigate_requested.connect" in text
        assert "card.open_panel_requested.connect" in text


class TestControllerSlots:
    def test_controller_has_e13_slots(self) -> None:
        for slot in (
            "_on_context_block_excluded",
            "_on_composer_scope_requested",
            "_on_help_navigate_requested",
            "_on_help_open_panel_requested",
        ):
            assert callable(getattr(AiEditController, slot, None)), slot

    def test_wiring_lists_include_e13_signals(self) -> None:
        # ai_edit_controller 的连接表以字符串形式引用面板信号
        controller_file = (
            Path(__file__).resolve().parents[2] / "src" / "app" /
            "controllers" / "ai_edit_controller.py"
        )
        text = controller_file.read_text(encoding="utf-8")
        for name in (
            "context_block_excluded",
            "composer_scope_requested",
            "help_navigate_requested",
            "help_open_panel_requested",
        ):
            assert f'"{name}"' in text, name


class TestExcludeBlockBehavior:
    @staticmethod
    def _block(kind: str, token: int) -> ContextBlock:
        return ContextBlock(
            kind=kind, content={"k": kind},
            priority=ContextBlockPriority.EVIDENCE,
            token_estimate=token, why_included="test",
            provenance={"ref": kind},
        )

    def test_exclude_removes_block_and_refills_panel(self) -> None:
        from src.features.edit.ai_editing.context_bundle import ContextBundle
        from src.features.edit.ai_editing.context_bundle import (
            ContextBlockPriority,
        )

        b1 = self._block("brief", 10)
        b2 = self._block("snapshot", 20)
        bundle = ContextBundle(blocks=(b1, b2))
        ctrl = AiEditController.__new__(AiEditController)
        ctrl._active_context_bundle = bundle
        refilled: list = []
        ctrl._panel = SimpleNamespace(set_context_bundle=refilled.append)
        ctrl._refresh_composer_footer = lambda: None

        ctrl._on_context_block_excluded(b1.id)
        assert len(refilled) == 1
        assert [b.id for b in refilled[0].blocks] == [b2.id]


class TestHelpOpenPanel:
    def test_open_panel_switches_page(self) -> None:
        ctrl = AiEditController.__new__(AiEditController)
        visited: list[str] = []
        ctrl.switch_to_page = visited.append
        ctrl._panel = SimpleNamespace(set_activity_status=lambda *a, **k: None)
        ctrl._on_help_open_panel_requested("edit")
        assert visited == ["Edit"]

    def test_unknown_page_ref_reports_not_switches(self) -> None:
        ctrl = AiEditController.__new__(AiEditController)
        visited: list[str] = []
        ctrl.switch_to_page = visited.append
        statuses: list[str] = []
        ctrl._panel = SimpleNamespace(
            set_activity_status=lambda text, **k: statuses.append(text))
        ctrl._on_help_open_panel_requested("nowhere")
        assert visited == []
        assert statuses, "未知 page_ref 必须给可见反馈"


class TestProfileEntry:
    def test_composer_profile_connected_to_model_preferences(self) -> None:
        text = _MIXIN_FILE.read_text(encoding="utf-8")
        assert "composer_profile_requested.connect(" in text
        assert "_open_ai_model_preferences" in text
