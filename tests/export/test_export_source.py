"""P5-T6：export_source 专测。

ExportSource Protocol（DORMANT，模块头注明暂无生产入口）的边界
语义验证：结构化实现可按协议调用、非实现者按属性缺失拒绝、
Protocol 非 runtime_checkable（isinstance 不可用）。接口本身无
运行时逻辑，测试聚焦契约形态而非行为分支。
"""

from __future__ import annotations

import pytest

from src.shared.export.export_source import ExportSource


class TimelineExportSource:
    """符合 ExportSource Protocol 的最小实现。"""

    def __init__(self) -> None:
        self.source_id = "timeline-main"
        self.title = "Main Timeline"

    def is_available(self) -> bool:
        return True

    def estimate_frames(self) -> int:
        return 240


class TestProtocolConformance:
    def test_conforming_implementation_is_callable_via_protocol(self) -> None:
        source = TimelineExportSource()
        # 通过协议注解使用（deliver 侧的预期用法）
        typed: ExportSource = source  # type: ignore[assignment]
        assert typed.source_id == "timeline-main"
        assert typed.title == "Main Timeline"
        assert typed.is_available() is True
        assert typed.estimate_frames() == 240

    def test_protocol_declares_expected_members(self) -> None:
        # 数据成员以协议注解声明，方法成员可在协议类上直接取到
        assert set(ExportSource.__annotations__) == {"source_id", "title"}
        for method in ("is_available", "estimate_frames"):
            assert callable(getattr(ExportSource, method, None))

    def test_missing_member_fails_on_use(self) -> None:
        class HalfImplemented:
            source_id = "half"

            def is_available(self) -> bool:
                return False
            # 缺 title / estimate_frames

        half = HalfImplemented()
        with pytest.raises(AttributeError):
            _ = half.title
        with pytest.raises(AttributeError):
            half.estimate_frames()

    def test_not_runtime_checkable(self) -> None:
        """未标注 @runtime_checkable：isinstance 检查显式拒绝。"""
        with pytest.raises(TypeError):
            isinstance(TimelineExportSource(), ExportSource)
