"""N17: 可选依赖统一探测（注入式；单一来源 fail-closed）。"""
from __future__ import annotations

from src.shared.diagnostics.optional_dependencies import (
    ai_model_available,
    optional_dependency_status,
    summarize,
)


def test_status_distinguishes_qt_runtime_and_external_tool() -> None:
    status = optional_dependency_status(
        tool_path=lambda name: "/opt/ffmpeg" if name == "ffmpeg" else None,
        ai_probe=lambda: {"pack_a": {"available": True},
                          "pack_b": {"available": False,
                                     "detail": "未安装"}},
        qt_probe=lambda: {"available": True, "source": "qt_runtime",
                          "detail": "stub"})
    assert status["qt_multimedia"]["available"] is True
    assert status["external_ffmpeg"]["available"] is True
    assert status["external_ffmpeg"]["detail"].startswith("path=/opt/ffmpeg")
    # 缺外部工具 ≠ Qt 不可用（语义分离）
    missing = optional_dependency_status(tool_path=lambda _n: None,
                                         ai_probe=lambda: {},
                                         qt_probe=lambda: {"available": True})
    assert missing["external_ffmpeg"]["available"] is False
    assert missing["qt_multimedia"]["available"] is True


def test_ai_model_gate_is_fail_closed() -> None:
    status = {"ai_model_packs": {"pack_a": {"available": True},
                                 "pack_b": {"available": False}}}
    assert ai_model_available("pack_a", status) is True
    assert ai_model_available("pack_b", status) is False  # 未安装≠通过
    assert ai_model_available("ghost", status) is False
    assert ai_model_available("pack_a", {"ai_model_packs": {}}) is False


def test_summary_is_single_line_and_scoped() -> None:
    status = optional_dependency_status(
        tool_path=lambda _n: None,
        ai_probe=lambda: {"vlm": {"available": False, "detail": "x"}},
        qt_probe=lambda: {"available": True, "source": "qt", "detail": "d"})
    line = summarize(status)
    assert "\n" not in line
    assert "external_ffmpeg=missing" in line and "vlm" in line
    assert "detail" not in line  # 不泄露探测细节


def test_real_probe_runs_without_external_tools() -> None:
    """无外部工具/模型环境：探测不抛、聚合可产出。"""
    status = optional_dependency_status(tool_path=lambda _n: None)
    assert isinstance(status["ai_model_packs"], dict)
    assert isinstance(summarize(status), str)
