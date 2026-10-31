"""
Video Performance Report Generator
视频性能报告生成器。

生成：
- JSON 报告（机器可读）
- Markdown 报告（人类可读）
- HTML 报告（可视化）

报告内容：
- 验收测试结果汇总
- 详细指标分析
- 性能趋势图表
- 通过/失败判定
"""

from __future__ import annotations

import json
import os
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from pathlib import Path

from tests.benchmark.acceptance_criteria import (
    AcceptanceReport,
    AcceptanceResult,
    AcceptanceLevel,
    METRICS,
    MetricCategory,
)


logger = logging.getLogger(__name__)


class ReportGenerator:
    """
    性能报告生成器。

    支持多种格式：
    - JSON: 结构化数据，用于自动化处理
    - Markdown: 文本报告，用于版本控制和文档
    - HTML: 可视化报告，用于展示和分析
    """

    def __init__(
        self,
        output_dir: str = "tests/benchmark/reports",
        report_prefix: str = "acceptance",
    ) -> None:
        """
        初始化报告生成器。

        Args:
            output_dir: 输出目录
            report_prefix: 报告文件前缀
        """
        self.output_dir = Path(output_dir)
        self.report_prefix = report_prefix

        # 确保输出目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_json_report(
        self,
        report: AcceptanceReport,
        filename: Optional[str] = None,
    ) -> str:
        """
        生成 JSON 报告。

        Args:
            report: 验收报告
            filename: 文件名（可选）

        Returns:
            JSON 文件路径
        """
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.report_prefix}_{timestamp}.json"

        filepath = self.output_dir / filename

        # 添加元数据
        report_dict = report.to_dict()
        report_dict['generator'] = {
            'version': '1.0.0',
            'generated_at': datetime.now().isoformat(),
            'tool': 'VideoPerformanceBenchmark',
        }

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report_dict, f, indent=2, ensure_ascii=False)

        logger.info(f"JSON 报告已生成: {filepath}")
        return str(filepath)

    def generate_markdown_report(
        self,
        report: AcceptanceReport,
        filename: Optional[str] = None,
    ) -> str:
        """
        生成 Markdown 报告。

        Args:
            report: 验收报告
            filename: 文件名（可选）

        Returns:
            Markdown 文件路径
        """
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.report_prefix}_{timestamp}.md"

        filepath = self.output_dir / filename

        content = self._build_markdown_content(report)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        logger.info(f"Markdown 报告已生成: {filepath}")
        return str(filepath)

    def _build_markdown_content(self, report: AcceptanceReport) -> str:
        """
        构建 Markdown 内容。

        Args:
            report: 验收报告

        Returns:
            Markdown 文本
        """
        lines: List[str] = []

        # 标题
        lines.append("# Video Performance Acceptance Report")
        lines.append(f"\n**生成时间**: {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"\n**整体结果**: {self._format_level(report.overall_level)}")
        lines.append(f"\n**通过率**: {report.summary['pass_rate']:.1%} ({report.summary['passed']}/{report.summary['total_metrics']})")

        # 汇总表
        lines.append("\n## Summary")
        lines.append("\n| 类别 | 通过 | 专业 | 失败 |")
        lines.append("|------|------|------|------|")

        by_category = report.summary.get('by_category', {})
        for cat, counts in by_category.items():
            lines.append(f"| {cat} | {counts['passed']} | {counts['professional']} | {counts['failed']} |")

        # 总计
        lines.append(f"| **Total** | {report.summary['passed']} | {report.summary['professional']} | {report.summary['failed']} |")

        # 详细结果
        lines.append("\n## Detailed Results")

        # 按类别分组
        for category in MetricCategory:
            cat_results = [
                (metric_id, result)
                for metric_id, result in report.results.items()
                if METRICS.get_metric(metric_id) and METRICS.get_metric(metric_id).category == category
            ]

            if not cat_results:
                continue

            lines.append(f"\n### {category.value.upper()}")

            # 表格头
            metric = METRICS.get_metric(cat_results[0][0])
            lines.append("\n| 指标 | 测量值 | 最低目标 | 专业目标 | 结果 |")
            lines.append("|------|--------|----------|----------|------|")

            for metric_id, result in cat_results:
                metric = METRICS.get_metric(metric_id)
                if metric:
                    unit = metric.unit
                    lines.append(
                        f"| {metric.name} | "
                        f"{self._format_value(result.measured_value, unit)} | "
                        f"{self._format_value(metric.minimum_target, unit)} | "
                        f"{self._format_value(metric.professional_target, unit)} | "
                        f"{self._format_level(result.level)} |"
                    )

        # 失败指标详情
        failed_results = [r for r in report.results.values() if not r.passed]
        if failed_results:
            lines.append("\n## Failed Metrics Analysis")
            lines.append("\n以下指标未达到最低目标：")

            for result in failed_results:
                metric = METRICS.get_metric(result.metric_id)
                if metric:
                    lines.append(f"\n### {metric.name}")
                    lines.append(f"\n- **测量值**: {self._format_value(result.measured_value, metric.unit)}")
                    lines.append(f"- **最低目标**: {self._format_value(metric.minimum_target, metric.unit)}")
                    lines.append(f"- **差距**: {self._format_value(result.measured_value - metric.minimum_target, metric.unit)}")
                    lines.append(f"\n**消息**: {result.message}")

                    # 详细数据
                    if result.details:
                        lines.append("\n**详细数据**:")
                        for key, value in result.details.items():
                            if isinstance(value, float):
                                lines.append(f"- {key}: {value:.2f}")
                            else:
                                lines.append(f"- {key}: {value}")

        # 测试环境信息
        lines.append("\n## Test Environment")
        if report.test_info:
            for key, value in report.test_info.items():
                lines.append(f"- **{key}**: {value}")
        else:
            lines.append("- 使用 MockPlaybackEnvironment 模拟测试环境")
            lines.append("- 测试平台: macOS / Linux / Windows")

        # 建议
        lines.append("\n## Recommendations")

        if report.overall_passed:
            if report.summary['professional_rate'] >= 0.8:
                lines.append("\n性能表现优秀，达到专业目标。建议保持当前配置。")
            else:
                lines.append("\n性能达标，但部分指标可进一步优化以达到专业目标。")
        else:
            lines.append("\n以下指标需要关注和优化：")

            for result in failed_results:
                metric = METRICS.get_metric(result.metric_id)
                if metric:
                    lines.append(f"\n### {metric.name}")
                    lines.append(self._generate_recommendation(metric.category, result))

        # 脚注
        lines.append("\n---")
        lines.append("\n*本报告由 Video Performance Acceptance Test Framework 自动生成*")

        return '\n'.join(lines)

    def _format_value(self, value: float, unit: str) -> str:
        """格式化测量值。"""
        if unit == "fps":
            return f"{value:.1f} fps"
        elif unit == "ms":
            return f"{value:.2f} ms"
        elif unit == "frames":
            return f"{value:.0f} frames"
        elif unit == "count":
            return f"{value:.0f}"
        elif unit == "rate":
            return f"{value:.1%}"
        elif unit == "ms/s":
            return f"{value:.2f} ms/s"
        else:
            return f"{value:.2f}"

    def _format_level(self, level: AcceptanceLevel) -> str:
        """格式化验收级别。"""
        if level == AcceptanceLevel.PROFESSIONAL:
            return "Professional Pass"
        elif level == AcceptanceLevel.MINIMUM:
            return "Minimum Pass"
        elif level == AcceptanceLevel.FAILED:
            return "Failed"
        else:
            return "Skipped"

    def _generate_recommendation(self, category: MetricCategory, result: AcceptanceResult) -> str:
        """生成优化建议。"""
        metric = METRICS.get_metric(result.metric_id)
        if not metric:
            return ""

        recommendations = {
            MetricCategory.PLAYBACK: [
                "- 检查解码器性能，考虑启用硬件加速",
                "- 降低代理分辨率以提高流畅度",
                "- 检查帧队列配置，确保预读缓冲充足",
                "- 优化颜色转换和格式转换流程",
            ],
            MetricCategory.UI_PERFORMANCE: [
                "- 减少 UI tick 中的同步操作",
                "- 优化事件处理逻辑，避免阻塞",
                "- 检查是否有不必要的 full render",
                "- 考虑使用 backpressure 机制降低队列深度",
            ],
            MetricCategory.RENDERER: [
                "- 优化渲染器队列管理",
                "- 启用渲染命令合并",
                "- 减少 GPU 上传延迟",
                "- 检查纹理池和 PBO 配置",
            ],
            MetricCategory.LATENCY: [
                "- 优化 seek 预读策略",
                "- 减少缓存驱逐",
                "- 检查是否有延迟累积问题",
                "- 考虑增加预读前瞻帧数",
            ],
            MetricCategory.INTERACTION: [
                "- 确保 zoom/pan 使用增量渲染",
                "- 避免 full render storm",
                "- 优化 ROI 计算和裁剪",
            ],
            MetricCategory.FALLBACK: [
                "- 检查 v3 管线稳定性",
                "- 修复导致 fallback 的根本问题",
                "- 确保硬件加速正常工作",
            ],
        }

        lines = ["建议优化措施："]
        for rec in recommendations.get(category, ["- 检查相关配置和实现"]):
            lines.append(rec)

        return '\n'.join(lines)

    def generate_html_report(
        self,
        report: AcceptanceReport,
        filename: Optional[str] = None,
    ) -> str:
        """
        生成 HTML 报告。

        Args:
            report: 验收报告
            filename: 文件名（可选）

        Returns:
            HTML 文件路径
        """
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.report_prefix}_{timestamp}.html"

        filepath = self.output_dir / filename

        content = self._build_html_content(report)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        logger.info(f"HTML 报告已生成: {filepath}")
        return str(filepath)

    def _build_html_content(self, report: AcceptanceReport) -> str:
        """构建 HTML 内容。"""
        # HTML 模板
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Video Performance Acceptance Report</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1000px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 20px;
        }}
        .summary {{
            display: flex;
            gap: 20px;
            margin-bottom: 30px;
        }}
        .summary-card {{
            background: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            flex: 1;
        }}
        .professional {{
            border-left: 4px solid #10b981;
        }}
        .minimum {{
            border-left: 4px solid #f59e0b;
        }}
        .failed {{
            border-left: 4px solid #ef4444;
        }}
        .metric-table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .metric-table th {{
            background: #667eea;
            color: white;
            padding: 12px;
            text-align: left;
        }}
        .metric-table td {{
            padding: 12px;
            border-bottom: 1px solid #eee;
        }}
        .metric-table tr:hover {{
            background: #f9f9f9;
        }}
        .pass {{
            color: #10b981;
        }}
        .fail {{
            color: #ef4444;
        }}
        .category {{
            margin-bottom: 30px;
        }}
        .category-title {{
            font-size: 1.2em;
            margin-bottom: 10px;
            padding-bottom: 5px;
            border-bottom: 2px solid #667eea;
        }}
        .recommendations {{
            background: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Video Performance Acceptance Report</h1>
        <p>生成时间: {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>

    <div class="summary">
        <div class="summary-card {report.overall_level.value}">
            <h3>整体结果</h3>
            <p class="{report.overall_passed and 'pass' or 'fail'}">
                {self._format_level(report.overall_level)}
            </p>
        </div>
        <div class="summary-card">
            <h3>通过率</h3>
            <p>{report.summary['pass_rate']:.1%}</p>
        </div>
        <div class="summary-card">
            <h3>专业达标率</h3>
            <p>{report.summary['professional_rate']:.1%}</p>
        </div>
    </div>

    {self._build_html_categories(report)}

    {self._build_html_recommendations(report)}

    <footer style="text-align: center; padding: 20px; color: #666;">
        <p>Video Performance Acceptance Test Framework</p>
    </footer>
</body>
</html>"""
        return html

    def _build_html_categories(self, report: AcceptanceReport) -> str:
        """构建 HTML 类别部分。"""
        sections = []

        for category in MetricCategory:
            cat_results = [
                (metric_id, result)
                for metric_id, result in report.results.items()
                if METRICS.get_metric(metric_id) and METRICS.get_metric(metric_id).category == category
            ]

            if not cat_results:
                continue

            section = f"""<div class="category">
    <div class="category-title">{category.value.upper()}</div>
    <table class="metric-table">
        <tr>
            <th>指标</th>
            <th>测量值</th>
            <th>最低目标</th>
            <th>专业目标</th>
            <th>结果</th>
        </tr>
"""

            for metric_id, result in cat_results:
                metric = METRICS.get_metric(metric_id)
                if metric:
                    pass_class = result.passed and 'pass' or 'fail'
                    unit = metric.unit
                    section += f"""        <tr>
            <td>{metric.name}</td>
            <td>{self._format_value(result.measured_value, unit)}</td>
            <td>{self._format_value(metric.minimum_target, unit)}</td>
            <td>{self._format_value(metric.professional_target, unit)}</td>
            <td class="{pass_class}">{self._format_level(result.level)}</td>
        </tr>
"""

            section += "    </table>\n</div>"
            sections.append(section)

        return '\n'.join(sections)

    def _build_html_recommendations(self, report: AcceptanceReport) -> str:
        """构建 HTML 建议部分。"""
        if report.overall_passed:
            return """<div class="recommendations">
    <h3>结论</h3>
    <p class="pass">性能验收测试通过，系统性能达标。</p>
</div>"""

        failed_results = [r for r in report.results.values() if not r.passed]

        html = """<div class="recommendations">
    <h3>优化建议</h3>
    <p class="fail">以下指标需要优化：</p>
    <ul>
"""

        for result in failed_results:
            metric = METRICS.get_metric(result.metric_id)
            if metric:
                html += f"""        <li>{metric.name}: {self._format_value(result.measured_value, metric.unit)} (目标: {self._format_value(metric.minimum_target, metric.unit)})</li>
"""

        html += """    </ul>
</div>"""
        return html

    def generate_all_reports(
        self,
        report: AcceptanceReport,
        timestamp: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        生成所有格式报告。

        Args:
            report: 验收报告
            timestamp: 时间戳（可选）

        Returns:
            文件路径字典 {'json': path, 'markdown': path, 'html': path}
        """
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        paths = {}

        paths['json'] = self.generate_json_report(
            report,
            f"{self.report_prefix}_{timestamp}.json"
        )

        paths['markdown'] = self.generate_markdown_report(
            report,
            f"{self.report_prefix}_{timestamp}.md"
        )

        paths['html'] = self.generate_html_report(
            report,
            f"{self.report_prefix}_{timestamp}.html"
        )

        return paths


def create_report_from_results(
    results: Dict[str, AcceptanceResult],
    test_info: Optional[Dict[str, Any]] = None,
) -> AcceptanceReport:
    """
    从测试结果创建验收报告。

    Args:
        results: 测试结果字典
        test_info: 测试环境信息

    Returns:
        AcceptanceReport
    """
    report = AcceptanceReport(test_info=test_info or {})

    for metric_id, result in results.items():
        report.add_result(result)

    return report


if __name__ == "__main__":
    # 示例：生成示例报告
    from tests.benchmark.run_acceptance import run_all_acceptance_tests

    results = run_all_acceptance_tests()
    report = create_report_from_results(results)

    generator = ReportGenerator()
    paths = generator.generate_all_reports(report)

    print("\n生成的报告文件：")
    for format_type, path in paths.items():
        print(f"  {format_type}: {path}")