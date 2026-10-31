#!/usr/bin/env python3
"""
Demo script for Video Performance Acceptance Test Framework.
演示验收测试报告生成器的功能。

运行方式（不需要完整依赖）：
    python3 tests/benchmark/demo_acceptance.py

生成示例报告：
    tests/benchmark/reports/acceptance_demo.json
    tests/benchmark/reports/acceptance_demo.md
    tests/benchmark/reports/acceptance_demo.html
"""

from __future__ import annotations

import sys
import os
import json
import time
import statistics
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum

# 确保输出目录存在
OUTPUT_DIR = Path(__file__).parent / "reports"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ================================================================
# 简化版验收指标定义
# ================================================================

class MetricCategory(Enum):
    PLAYBACK = "playback"
    UI_PERFORMANCE = "ui_perf"
    RENDERER = "renderer"
    LATENCY = "latency"
    INTERACTION = "interaction"
    FALLBACK = "fallback"


class AcceptanceLevel(Enum):
    PROFESSIONAL = "professional"
    MINIMUM = "minimum"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class MockMetric:
    id: str
    name: str
    category: MetricCategory
    unit: str
    minimum_target: float
    professional_target: float
    notes: str = ""


@dataclass
class MockResult:
    metric_id: str
    measured_value: float
    level: AcceptanceLevel
    details: Dict[str, Any] = field(default_factory=dict)
    message: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def passed(self) -> bool:
        return self.level in (AcceptanceLevel.PROFESSIONAL, AcceptanceLevel.MINIMUM)

    @property
    def professional_passed(self) -> bool:
        return self.level == AcceptanceLevel.PROFESSIONAL


# P5-3 验收指标表
METRICS: Dict[str, MockMetric] = {
    "playback_1080p_h264_fps": MockMetric(
        id="playback_1080p_h264_fps",
        name="1080p H.264 Proxy Playback FPS",
        category=MetricCategory.PLAYBACK,
        unit="fps",
        minimum_target=24.0,
        professional_target=60.0,
    ),
    "playback_4k_h264_fps": MockMetric(
        id="playback_4k_h264_fps",
        name="4K H.264/H.265 Proxy Playback FPS",
        category=MetricCategory.PLAYBACK,
        unit="fps",
        minimum_target=24.0,
        professional_target=60.0,
        notes="视机器性能",
    ),
    "playback_4k_prores_fps": MockMetric(
        id="playback_4k_prores_fps",
        name="4K ProRes Proxy Playback FPS",
        category=MetricCategory.PLAYBACK,
        unit="fps",
        minimum_target=24.0,
        professional_target=60.0,
        notes="视磁盘/CPU",
    ),
    "ui_tick_p95_ms": MockMetric(
        id="ui_tick_p95_ms",
        name="UI Tick P95 Latency",
        category=MetricCategory.UI_PERFORMANCE,
        unit="ms",
        minimum_target=5.0,
        professional_target=2.0,
    ),
    "ui_tick_max_ms": MockMetric(
        id="ui_tick_max_ms",
        name="UI Tick Max Latency",
        category=MetricCategory.UI_PERFORMANCE,
        unit="ms",
        minimum_target=20.0,
        professional_target=10.0,
    ),
    "renderer_queue_depth_p95": MockMetric(
        id="renderer_queue_depth_p95",
        name="Renderer Queue Depth P95",
        category=MetricCategory.RENDERER,
        unit="frames",
        minimum_target=3.0,
        professional_target=2.0,
    ),
    "renderer_queue_max_depth": MockMetric(
        id="renderer_queue_max_depth",
        name="Renderer Queue Max Depth",
        category=MetricCategory.RENDERER,
        unit="frames",
        minimum_target=5.0,
        professional_target=3.0,
    ),
    "playback_latency_growth_rate": MockMetric(
        id="playback_latency_growth_rate",
        name="Playback Latency Growth Rate",
        category=MetricCategory.LATENCY,
        unit="ms/s",
        minimum_target=1.0,
        professional_target=0.0,
    ),
    "seek_latency_p95_ms": MockMetric(
        id="seek_latency_p95_ms",
        name="Seek Latency P95",
        category=MetricCategory.LATENCY,
        unit="ms",
        minimum_target=100.0,
        professional_target=50.0,
    ),
    "zoom_pan_render_storm_count": MockMetric(
        id="zoom_pan_render_storm_count",
        name="Zoom/Pan Render Storm Count",
        category=MetricCategory.INTERACTION,
        unit="count",
        minimum_target=0.0,
        professional_target=0.0,
    ),
    "zoom_pan_response_ms": MockMetric(
        id="zoom_pan_response_ms",
        name="Zoom/Pan Response Time",
        category=MetricCategory.INTERACTION,
        unit="ms",
        minimum_target=50.0,
        professional_target=16.0,
    ),
    "fallback_v2_count": MockMetric(
        id="fallback_v2_count",
        name="Fallback V2 Count",
        category=MetricCategory.FALLBACK,
        unit="count",
        minimum_target=1.0,
        professional_target=0.0,
    ),
    "fallback_rate": MockMetric(
        id="fallback_rate",
        name="Fallback Rate",
        category=MetricCategory.FALLBACK,
        unit="rate",
        minimum_target=0.05,
        professional_target=0.0,
    ),
}


# ================================================================
# 模拟测试结果生成
# ================================================================

def generate_demo_results() -> Dict[str, MockResult]:
    """生成演示测试结果。"""
    results: Dict[str, MockResult] = {}

    # 模拟播放测试结果
    results["playback_1080p_h264_fps"] = MockResult(
        metric_id="playback_1080p_h264_fps",
        measured_value=45.5,
        level=AcceptanceLevel.MINIMUM,
        details={
            "fps_avg": 45.5,
            "fps_min": 38.2,
            "fps_max": 52.1,
            "fps_std": 4.3,
            "tick_avg_ms": 22.0,
            "decode_avg_ms": 12.0,
            "test_duration_s": 10.0,
        },
        message="1080p H.264 播放稳定，平均帧率 45.5fps",
    )

    results["playback_4k_h264_fps"] = MockResult(
        metric_id="playback_4k_h264_fps",
        measured_value=32.0,
        level=AcceptanceLevel.MINIMUM,
        details={
            "fps_avg": 32.0,
            "fps_min": 26.5,
            "fps_max": 38.2,
            "fps_std": 3.8,
            "tick_avg_ms": 31.3,
            "decode_avg_ms": 18.0,
        },
        message="4K H.264 播放稳定，达到最低目标",
    )

    results["playback_4k_prores_fps"] = MockResult(
        metric_id="playback_4k_prores_fps",
        measured_value=28.5,
        level=AcceptanceLevel.MINIMUM,
        details={
            "fps_avg": 28.5,
            "fps_std": 5.2,
            "disk_io_avg_ms": 25.0,
        },
        message="4K ProRes 播放受磁盘限制，勉强达标",
    )

    # UI tick 测试结果
    results["ui_tick_p95_ms"] = MockResult(
        metric_id="ui_tick_p95_ms",
        measured_value=3.5,
        level=AcceptanceLevel.MINIMUM,  # 反向指标
        details={
            "tick_avg_ms": 2.1,
            "tick_p50_ms": 1.8,
            "tick_p95_ms": 3.5,
            "tick_max_ms": 12.0,
            "sample_count": 600,
        },
        message="UI tick P95 在正常范围",
    )

    results["ui_tick_max_ms"] = MockResult(
        metric_id="ui_tick_max_ms",
        measured_value=12.0,
        level=AcceptanceLevel.MINIMUM,  # 反向指标
        details={"spike_count": 3},
        message="偶有 GC spike，整体可接受",
    )

    # 渲染队列测试结果
    results["renderer_queue_depth_p95"] = MockResult(
        metric_id="renderer_queue_depth_p95",
        measured_value=2.5,
        level=AcceptanceLevel.PROFESSIONAL,  # 反向指标
        details={
            "queue_avg_depth": 1.8,
            "queue_max_depth": 4,
        },
        message="渲染队列深度控制良好",
    )

    results["renderer_queue_max_depth"] = MockResult(
        metric_id="renderer_queue_max_depth",
        measured_value=4.0,
        level=AcceptanceLevel.MINIMUM,  # 反向指标
        details={},
        message="队列最大深度未超标",
    )

    # 延迟测试结果
    results["playback_latency_growth_rate"] = MockResult(
        metric_id="playback_latency_growth_rate",
        measured_value=0.3,
        level=AcceptanceLevel.PROFESSIONAL,  # 反向指标
        details={
            "latency_avg_ms": 12.0,
            "latency_max_ms": 25.0,
        },
        message="播放延迟几乎无增长",
    )

    results["seek_latency_p95_ms"] = MockResult(
        metric_id="seek_latency_p95_ms",
        measured_value=65.0,
        level=AcceptanceLevel.MINIMUM,  # 反向指标
        details={
            "seek_avg_ms": 45.0,
            "seek_max_ms": 120.0,
            "slow_seek_count": 2,
        },
        message="Seek 延迟达标，偶有慢 seek",
    )

    # 交互测试结果
    results["zoom_pan_render_storm_count"] = MockResult(
        metric_id="zoom_pan_render_storm_count",
        measured_value=0.0,
        level=AcceptanceLevel.PROFESSIONAL,
        details={},
        message="Zoom/Pan 无 render storm",
    )

    results["zoom_pan_response_ms"] = MockResult(
        metric_id="zoom_pan_response_ms",
        measured_value=18.0,
        level=AcceptanceLevel.MINIMUM,  # 反向指标
        details={},
        message="Zoom/Pan 响应时间达标",
    )

    # Fallback 测试结果
    results["fallback_v2_count"] = MockResult(
        metric_id="fallback_v2_count",
        measured_value=0.0,
        level=AcceptanceLevel.PROFESSIONAL,
        details={},
        message="无 Fallback 事件发生",
    )

    results["fallback_rate"] = MockResult(
        metric_id="fallback_rate",
        measured_value=0.0,
        level=AcceptanceLevel.PROFESSIONAL,
        details={},
        message="Fallback 率为 0%",
    )

    return results


# ================================================================
# 报告生成
# ================================================================

def generate_json_report(results: Dict[str, MockResult]) -> str:
    """生成 JSON 报告。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = OUTPUT_DIR / f"acceptance_demo_{timestamp}.json"

    # 构建报告数据
    passed = sum(1 for r in results.values() if r.passed)
    professional = sum(1 for r in results.values() if r.professional_passed)
    failed = sum(1 for r in results.values() if not r.passed)
    total = len(results)

    report = {
        "results": {
            k: {
                "metric_id": v.metric_id,
                "measured_value": v.measured_value,
                "level": v.level.value,
                "passed": v.passed,
                "professional_passed": v.professional_passed,
                "details": v.details,
                "message": v.message,
                "timestamp": v.timestamp.isoformat(),
            }
            for k, v in results.items()
        },
        "summary": {
            "total_metrics": total,
            "passed": passed,
            "failed": failed,
            "professional": professional,
            "pass_rate": passed / total,
            "professional_rate": professional / total,
        },
        "overall_passed": failed == 0,
        "overall_level": "professional" if professional / total >= 0.8 else ("minimum" if passed / total >= 1.0 else "failed"),
        "generated_at": datetime.now().isoformat(),
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    return str(filepath)


def generate_markdown_report(results: Dict[str, MockResult]) -> str:
    """生成 Markdown 报告。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = OUTPUT_DIR / f"acceptance_demo_{timestamp}.md"

    passed = sum(1 for r in results.values() if r.passed)
    professional = sum(1 for r in results.values() if r.professional_passed)
    failed = sum(1 for r in results.values() if not r.passed)
    total = len(results)

    lines = [
        "# Video Performance Acceptance Report (Demo)",
        "",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"**整体结果**: {'PASS' if failed == 0 else 'FAIL'}",
        "",
        f"**通过率**: {passed}/{total} ({passed/total:.1%})",
        "",
        f"**专业达标率**: {professional}/{total} ({professional/total:.1%})",
        "",
        "## Summary",
        "",
        "| 类别 | 通过 | 专业 | 失败 |",
        "|------|------|------|------|",
    ]

    # 按类别统计
    by_category: Dict[str, Dict[str, int]] = {}
    for metric_id, result in results.items():
        metric = METRICS[metric_id]
        cat = metric.category.value
        if cat not in by_category:
            by_category[cat] = {"passed": 0, "professional": 0, "failed": 0}
        if result.passed:
            by_category[cat]["passed"] += 1
        else:
            by_category[cat]["failed"] += 1
        if result.professional_passed:
            by_category[cat]["professional"] += 1

    for cat, counts in by_category.items():
        lines.append(f"| {cat} | {counts['passed']} | {counts['professional']} | {counts['failed']} |")

    lines.append(f"| **Total** | {passed} | {professional} | {failed} |")

    # 详细结果
    lines.append("")
    lines.append("## Detailed Results")

    for category in MetricCategory:
        cat_results = [(k, v) for k, v in results.items() if METRICS[k].category == category]
        if not cat_results:
            continue

        lines.append("")
        lines.append(f"### {category.value.upper()}")
        lines.append("")
        lines.append("| 指标 | 测量值 | 最低目标 | 专业目标 | 结果 |")
        lines.append("|------|--------|----------|----------|------|")

        for metric_id, result in cat_results:
            metric = METRICS[metric_id]
            status = "专业达标" if result.professional_passed else ("最低达标" if result.passed else "失败")
            lines.append(f"| {metric.name} | {result.measured_value:.1f} {metric.unit} | {metric.minimum_target:.1f} {metric.unit} | {metric.professional_target:.1f} {metric.unit} | {status} |")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*本报告由 Video Performance Acceptance Test Framework 演示生成*")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write('\n'.join(lines))

    return str(filepath)


def generate_html_report(results: Dict[str, MockResult]) -> str:
    """生成 HTML 报告。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = OUTPUT_DIR / f"acceptance_demo_{timestamp}.html"

    passed = sum(1 for r in results.values() if r.passed)
    professional = sum(1 for r in results.values() if r.professional_passed)
    failed = sum(1 for r in results.values() if not r.passed)
    total = len(results)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>Video Performance Acceptance Report</title>
    <style>
        body {{ font-family: system-ui; max-width: 900px; margin: auto; padding: 20px; }}
        .header {{ background: #667eea; color: white; padding: 20px; border-radius: 8px; }}
        .summary {{ display: flex; gap: 15px; margin: 20px 0; }}
        .card {{ background: white; padding: 15px; border-radius: 8px; flex: 1; }}
        .pass {{ color: #10b981; }}
        .fail {{ color: #ef4444; }}
        table {{ width: 100%; border-collapse: collapse; background: white; }}
        th {{ background: #667eea; color: white; padding: 10px; }}
        td {{ padding: 10px; border-bottom: 1px solid #eee; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Video Performance Acceptance Report</h1>
        <p>Demo Report - Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>

    <div class="summary">
        <div class="card"><h3>Overall</h3><p class="{'pass' if failed == 0 else 'fail'}">{'PASS' if failed == 0 else 'FAIL'}</p></div>
        <div class="card"><h3>Pass Rate</h3><p>{passed/total:.1%}</p></div>
        <div class="card"><h3>Professional</h3><p>{professional/total:.1%}</p></div>
    </div>

    <table>
        <tr><th>Category</th><th>Metric</th><th>Measured</th><th>Min Target</th><th>Pro Target</th><th>Status</th></tr>
"""

    for metric_id, result in results.items():
        metric = METRICS[metric_id]
        status_class = "pass" if result.passed else "fail"
        status = "Pro" if result.professional_passed else ("Min" if result.passed else "Fail")
        html += f"""        <tr>
            <td>{metric.category.value}</td>
            <td>{metric.name}</td>
            <td>{result.measured_value:.1f} {metric.unit}</td>
            <td>{metric.minimum_target:.1f} {metric.unit}</td>
            <td>{metric.professional_target:.1f} {metric.unit}</td>
            <td class="{status_class}">{status}</td>
        </tr>
"""

    html += """    </table>
    <footer style="text-align: center; padding: 20px; color: #666;">
        Video Performance Acceptance Test Framework - Demo
    </footer>
</body>
</html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    return str(filepath)


def main() -> int:
    """运行演示。"""
    print("\n" + "=" * 60)
    print("Video Performance Acceptance Test Framework Demo")
    print("=" * 60)

    # 生成演示结果
    print("\n生成演示测试结果...")
    results = generate_demo_results()

    # 打印汇总
    passed = sum(1 for r in results.values() if r.passed)
    professional = sum(1 for r in results.values() if r.professional_passed)
    failed = sum(1 for r in results.values() if not r.passed)
    total = len(results)

    print(f"\n汇总统计:")
    print(f"  总计: {total} 项指标")
    print(f"  通过: {passed} ({passed/total:.1%})")
    print(f"  专业达标: {professional} ({professional/total:.1%})")
    print(f"  失败: {failed}")

    print("\n详细结果:")
    for metric_id, result in results.items():
        metric = METRICS[metric_id]
        status = "专业" if result.professional_passed else ("最低" if result.passed else "失败")
        print(f"  [{status}] {metric.name}: {result.measured_value:.1f} {metric.unit}")

    # 生成报告
    print("\n生成报告文件...")

    json_path = generate_json_report(results)
    print(f"  JSON: {json_path}")

    md_path = generate_markdown_report(results)
    print(f"  Markdown: {md_path}")

    html_path = generate_html_report(results)
    print(f"  HTML: {html_path}")

    print("\n演示完成！")
    print(f"\n报告已保存到: {OUTPUT_DIR}")

    return 0


if __name__ == "__main__":
    sys.exit(main())