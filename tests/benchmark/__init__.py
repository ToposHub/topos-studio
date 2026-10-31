"""
Video Performance Acceptance Tests
视频性能验收测试框架。

用于验证 P5-3 视频优化方案的验收指标。

目录结构：
- acceptance_criteria.py: 验收指标定义
- test_playback_performance.py: 播放性能测试
- test_ui_tick.py: UI tick 性能测试
- test_renderer_queue.py: 渲染队列测试
- test_latency.py: 延迟和 fallback 测试
- run_acceptance.py: 一键运行所有验收测试
- report_generator.py: 性能报告生成器
"""

from tests.benchmark.acceptance_criteria import (
    AcceptanceMetric,
    AcceptanceCriteria,
    AcceptanceResult,
    AcceptanceReport,
    METRICS,
)

__all__ = [
    'AcceptanceMetric',
    'AcceptanceCriteria',
    'AcceptanceResult',
    'AcceptanceReport',
    'METRICS',
]