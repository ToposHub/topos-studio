"""宿主接线门禁（第五轮审计 E22 计划第 4 点）。

防止再次出现"UI 有入口、宿主没接线"的腐化：

1. 本轮（E02/E04/E06/E08/E09/E10/E13）接线的信号必须保持有接收者——
   一旦有人删掉宿主 connect，此测试立即失败；
2. E22 删除的死信号必须保持删除——不允许"有 emit 无 connect"的死路径
   复活。

静态扫描说明：getattr 风格连接（``getattr(panel, "sig", None)`` 再
connect）无法用 ``.sig.connect(`` 正则匹配，因此接收者判定同时接受
"直接 connect" 与 "信号名以字符串字面量出现在 src 中（getattr 装配）"
两种形态。该判定只用于下列已知信号，不做全仓推广（全仓启发式误报率高，
见 docs/edit_page_fifth_audit_optimization_plan_2026-10-20.md E22）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SRC = Path(__file__).resolve().parents[2] / "src"


def _src_text() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in SRC.rglob("*.py")
    )


def _signal_has_receiver(name: str, text: str) -> bool:
    if re.search(rf"\.{re.escape(name)}\.connect\(", text):
        return True
    # getattr 装配：信号名以字符串字面量出现（ai_edit_controller 的
    # tuple 连接表 / hasattr 门控 connect 均为此形态）
    if re.search(rf"[\"']{re.escape(name)}[\"']", text):
        return True
    return False


class TestAuditedSignalsKeepReceivers:
    """本轮接线的信号不得退回"无接收者"状态。"""

    WIRED_SIGNALS = [
        # (信号名, 审计项)
        ("status_message_requested", "E04 时间线错误反馈"),
        ("playback_state_changed", "E06 播放状态桥接"),
        ("open_asset_grade_requested", "E08 图片调色入口"),
        ("clip_open_in_source_requested", "E09 片段双击"),
        ("add_transition_requested", "E10 转场右键"),
        ("keyframe_toggled", "E02 片段关键帧"),
        ("effect_keyframe_toggled", "E02 效果关键帧"),
        ("context_block_excluded", "E13 上下文排除"),
        ("composer_scope_requested", "E13 /scope"),
        ("composer_profile_requested", "E13 profile 入口"),
        ("help_navigate_requested", "E13 帮助定位"),
        ("help_open_panel_requested", "E13 帮助切页"),
    ]

    def test_wired_signals_have_receivers(self) -> None:
        text = _src_text()
        missing = [
            f"{name} ({item})"
            for name, item in self.WIRED_SIGNALS
            if not _signal_has_receiver(name, text)
        ]
        assert not missing, (
            "以下信号回退为无接收者（宿主 connect 被移除？）：" + ", ".join(missing)
        )


class TestRemovedDeadSignalsStayRemoved:
    """E22 删除的死信号不得复活。"""

    @staticmethod
    def _class_has_signal(cls: type, name: str) -> bool:
        return name in vars(cls)

    def test_removed_signals_absent(self) -> None:
        from src.features.edit.ui.quick_project_dialog import QuickProjectDialog
        from src.features.edit.ui.source_viewer import SourceViewerWidget
        from src.features.edit.ui.timeline_items import TrackHeaderItem
        from src.features.edit.ui.timeline_view import TimelineView

        cases = [
            (QuickProjectDialog, "quick_project_created"),
            (SourceViewerWidget, "source_opened"),
            (SourceViewerWidget, "source_closed"),
            (TrackHeaderItem, "track_double_clicked"),
            (TimelineView, "command_executed"),
        ]
        present = [
            f"{cls.__name__}.{name}"
            for cls, name in cases
            if self._class_has_signal(cls, name)
        ]
        assert not present, "E22 已删除的死信号重新出现：" + ", ".join(present)

    def test_removed_hub_signals_absent(self) -> None:
        from src.features.edit.ui.edit_page import EditSignalHub

        present = [
            name for name in (
                "asset_removed",
                "insert_requested",
                "overwrite_requested",
                "append_requested",
            )
            if self._class_has_signal(EditSignalHub, name)
        ]
        assert not present, "EditSignalHub 死信号重新出现：" + ", ".join(present)

    def test_no_emit_without_connect_for_removed_names(self) -> None:
        """被删信号名不得再出现 .emit( 调用。"""
        text = _src_text()
        revived = [
            name for name in (
                "asset_removed",
                "quick_project_created",
                "source_opened",
                "source_closed",
                "track_double_clicked",
                "command_executed",
            )
            if re.search(rf"\.{re.escape(name)}\.emit\(", text)
        ]
        assert not revived, "已删信号出现 emit 调用：" + ", ".join(revived)
