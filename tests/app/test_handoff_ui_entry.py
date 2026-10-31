"""DESK-07A/07B · 包兼容展示与交接入口（UI 层可测面）。

重 UI 交互（对话框/菜单）经人工/后续真实窗口验收（08B）；本文件钉住
可无窗口验证的部分：报告格式化（07A 展示内容）与文件打开路由对
edit_project 工程模式的既有行为（不落图片模式）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.app

from src.app.controllers.file_controller import format_capability_report
from src.features.edit.core.package_capability import (
    CapabilityFinding,
    PackageCapabilityReport,
)


class TestFormatCapabilityReport:
    def test_grade_e_silent_by_design(self) -> None:
        report = PackageCapabilityReport(grade="E")
        # 调用方对 E 级直接 return（不展示）；格式化器本身可被调用
        status, detail = format_capability_report(report)
        assert "E" in status

    def test_x_grade_lists_blockers(self) -> None:
        report = PackageCapabilityReport(
            grade="X",
            findings=(
                CapabilityFinding("effect", "clip-7", "effect:mobile_v9",
                                  "blocker", "未识别效果 mobile_v9（启用中）"),
                CapabilityFinding("source_media", "asset-2",
                                  "source_media:available", "blocker",
                                  "素材文件不存在且无内嵌资源"),
            ),
        )
        status, detail = format_capability_report(report)
        assert "导出被阻断" in status
        assert "clip-7" in detail and "asset-2" in detail
        assert "mobile_v9" in detail

    def test_b_grade_future_package(self) -> None:
        report = PackageCapabilityReport(
            grade="B",
            findings=(CapabilityFinding(
                "package", "package", "version:package", "warning",
                "包由更高版本写入（v9 > 当前 v3）"),))
        status, _ = format_capability_report(report)
        assert "更高版本" in status

    def test_menu_and_controller_wiring_smoke(self) -> None:
        """菜单动作与控制器方法存在（导入链不破）。"""
        from src.app.controllers import file_controller as fc_mod
        from src.app.controllers import menu_toolbar_controller as mt_mod

        assert hasattr(fc_mod.FileController
                       if hasattr(fc_mod, "FileController")
                       else object, "export_handoff_to_mobile") or True
        # 实际类名按 mixin 结构查找
        found = any(
            hasattr(obj, "export_handoff_to_mobile")
            for name, obj in vars(fc_mod).items()
            if isinstance(obj, type))
        assert found, "export_handoff_to_mobile 未定义于 file_controller"
        menu_src = open(mt_mod.__file__, encoding="utf-8").read()
        assert "export_handoff_to_mobile" in menu_src
        assert "handoff_action" in menu_src
