"""
Video Performance Acceptance Criteria
视频性能验收指标定义。

定义 P5-3 视频优化方案的验收指标：
- 最低目标：必须达成
- 专业目标：追求达成

验收指标表：
| 指标 | 最低目标 | 专业目标 |
|---|---|---|
| 1080p H.264 proxy playback | 稳定 24/30fps | 稳定 60fps |
| 4K H.264/H.265 proxy playback | 稳定 24/30fps | 低延迟 60fps 视机器 |
| 4K ProRes proxy playback | 稳定 24/30fps | 30/60fps 视磁盘/CPU |
| UI tick p95 | < 5ms | < 2ms |
| renderer queue depth p95 | <= 3 | <= 2 |
| playback latency | 不持续增长 | 始终低延迟 |
| zoom/pan during playback | 无 full render storm | 丝滑 |
| fallback v2 | 只在明确失败时 | 几乎不发生 |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Any, Callable
from datetime import datetime
import json


class MetricCategory(Enum):
    """验收指标类别。"""
    PLAYBACK = "playback"       # 播放流畅度
    UI_PERFORMANCE = "ui_perf"  # UI 性能
    RENDERER = "renderer"       # 渲染器状态
    LATENCY = "latency"         # 延迟指标
    INTERACTION = "interaction" # 交互响应
    FALLBACK = "fallback"       # Fallback 状态


class AcceptanceLevel(Enum):
    """验收结果级别。"""
    PROFESSIONAL = "professional"  # 达成专业目标
    MINIMUM = "minimum"            # 达成最低目标
    FAILED = "failed"              # 未达成最低目标
    SKIPPED = "skipped"            # 测试跳过


@dataclass
class AcceptanceMetric:
    """
    单个验收指标定义。

    Attributes:
        id: 指标唯一标识
        name: 指标名称
        category: 指标类别
        description: 详细描述
        unit: 单位（fps, ms, frames, count, rate）
        minimum_target: 最低目标值
        professional_target: 专业目标值
        measurement_func: 测量方法描述
        notes: 备注（如"视机器"、"视磁盘/CPU"）
    """
    id: str
    name: str
    category: MetricCategory
    description: str
    unit: str
    minimum_target: float
    professional_target: float
    measurement_func: str = ""
    notes: str = ""

    def evaluate(self, measured_value: float) -> AcceptanceLevel:
        """
        评估测量值是否达标。

        Args:
            measured_value: 实际测量值

        Returns:
            AcceptanceLevel: 验收结果级别
        """
        if measured_value >= self.professional_target:
            return AcceptanceLevel.PROFESSIONAL
        elif measured_value >= self.minimum_target:
            return AcceptanceLevel.MINIMUM
        else:
            return AcceptanceLevel.FAILED

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            'id': self.id,
            'name': self.name,
            'category': self.category.value,
            'description': self.description,
            'unit': self.unit,
            'minimum_target': self.minimum_target,
            'professional_target': self.professional_target,
            'measurement_func': self.measurement_func,
            'notes': self.notes,
        }


@dataclass
class AcceptanceResult:
    """
    单个验收指标测试结果。

    Attributes:
        metric_id: 指标 ID
        measured_value: 测量值
        level: 验收级别
        details: 详细数据（如时间序列、分布）
        message: 结果描述
        timestamp: 测试时间
        test_duration_ms: 测试耗时（毫秒）
    """
    metric_id: str
    measured_value: float
    level: AcceptanceLevel
    details: Dict[str, Any] = field(default_factory=dict)
    message: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    test_duration_ms: float = 0.0

    @property
    def passed(self) -> bool:
        """是否通过最低目标。"""
        return self.level in (AcceptanceLevel.PROFESSIONAL, AcceptanceLevel.MINIMUM)

    @property
    def professional_passed(self) -> bool:
        """是否通过专业目标。"""
        return self.level == AcceptanceLevel.PROFESSIONAL

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            'metric_id': self.metric_id,
            'measured_value': self.measured_value,
            'level': self.level.value,
            'passed': self.passed,
            'professional_passed': self.professional_passed,
            'details': self.details,
            'message': self.message,
            'timestamp': self.timestamp.isoformat(),
            'test_duration_ms': self.test_duration_ms,
        }


@dataclass
class AcceptanceCriteria:
    """
    验收标准集合。

    包含所有验收指标的定义。
    """
    metrics: Dict[str, AcceptanceMetric] = field(default_factory=dict)

    def add_metric(self, metric: AcceptanceMetric) -> None:
        """添加指标。"""
        self.metrics[metric.id] = metric

    def get_metric(self, metric_id: str) -> Optional[AcceptanceMetric]:
        """获取指标。"""
        return self.metrics.get(metric_id)

    def get_metrics_by_category(self, category: MetricCategory) -> List[AcceptanceMetric]:
        """按类别获取指标。"""
        return [m for m in self.metrics.values() if m.category == category]

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            'metrics': {k: v.to_dict() for k, v in self.metrics.items()},
        }


@dataclass
class AcceptanceReport:
    """
    验收测试报告。

    Attributes:
        results: 所有指标测试结果
        summary: 汇总统计
        overall_passed: 整体是否通过
        overall_level: 整体验收级别
        test_info: 测试环境信息
        generated_at: 报告生成时间
    """
    results: Dict[str, AcceptanceResult] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)
    overall_passed: bool = False
    overall_level: AcceptanceLevel = AcceptanceLevel.FAILED
    test_info: Dict[str, Any] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=datetime.now)

    def add_result(self, result: AcceptanceResult) -> None:
        """添加结果。"""
        self.results[result.metric_id] = result
        self._update_summary()

    def _update_summary(self) -> None:
        """更新汇总统计。"""
        passed_count = sum(1 for r in self.results.values() if r.passed)
        professional_count = sum(1 for r in self.results.values() if r.professional_passed)
        failed_count = sum(1 for r in self.results.values() if not r.passed)
        total_count = len(self.results)

        # 计算通过率
        pass_rate = passed_count / total_count if total_count > 0 else 0.0
        professional_rate = professional_count / total_count if total_count > 0 else 0.0

        self.summary = {
            'total_metrics': total_count,
            'passed': passed_count,
            'failed': failed_count,
            'professional': professional_count,
            'pass_rate': pass_rate,
            'professional_rate': professional_rate,
            'by_category': self._count_by_category(),
        }

        # 整体通过判断
        self.overall_passed = failed_count == 0

        # 整体级别判断
        if professional_rate >= 0.8:
            self.overall_level = AcceptanceLevel.PROFESSIONAL
        elif pass_rate >= 1.0:
            self.overall_level = AcceptanceLevel.MINIMUM
        else:
            self.overall_level = AcceptanceLevel.FAILED

    def _count_by_category(self) -> Dict[str, Dict[str, int]]:
        """按类别统计。"""
        # 需要有 METRICS 定义才能分类
        from tests.benchmark.acceptance_criteria import METRICS
        counts: Dict[str, Dict[str, int]] = {}
        for result in self.results.values():
            metric = METRICS.get_metric(result.metric_id)
            if metric:
                cat = metric.category.value
                if cat not in counts:
                    counts[cat] = {'passed': 0, 'failed': 0, 'professional': 0}
                if result.passed:
                    counts[cat]['passed'] += 1
                else:
                    counts[cat]['failed'] += 1
                if result.professional_passed:
                    counts[cat]['professional'] += 1
        return counts

    def to_json(self) -> str:
        """转换为 JSON。"""
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            'results': {k: v.to_dict() for k, v in self.results.items()},
            'summary': self.summary,
            'overall_passed': self.overall_passed,
            'overall_level': self.overall_level.value,
            'test_info': self.test_info,
            'generated_at': self.generated_at.isoformat(),
        }


# ================================================================
# P5-3 验收指标定义
# ================================================================

METRICS = AcceptanceCriteria()

# 播放流畅度指标
METRICS.add_metric(AcceptanceMetric(
    id="playback_1080p_h264_fps",
    name="1080p H.264 Proxy Playback FPS",
    category=MetricCategory.PLAYBACK,
    description="1080p H.264 代理视频播放帧率",
    unit="fps",
    minimum_target=24.0,
    professional_target=60.0,
    measurement_func="播放 1080p H.264 视频 30 秒，统计有效帧率",
    notes="稳定帧率，无明显波动",
))

METRICS.add_metric(AcceptanceMetric(
    id="playback_4k_h264_fps",
    name="4K H.264/H.265 Proxy Playback FPS",
    category=MetricCategory.PLAYBACK,
    description="4K H.264/H.265 代理视频播放帧率",
    unit="fps",
    minimum_target=24.0,
    professional_target=60.0,
    measurement_func="播放 4K H.264/H.265 视频 30 秒，统计有效帧率",
    notes="视机器性能，专业目标为低延迟 60fps",
))

METRICS.add_metric(AcceptanceMetric(
    id="playback_4k_prores_fps",
    name="4K ProRes Proxy Playback FPS",
    category=MetricCategory.PLAYBACK,
    description="4K ProRes 代理视频播放帧率",
    unit="fps",
    minimum_target=24.0,
    professional_target=60.0,
    measurement_func="播放 4K ProRes 视频 30 秒，统计有效帧率",
    notes="视磁盘/CPU 性能，专业目标为 30/60fps",
))

# UI 性能指标
METRICS.add_metric(AcceptanceMetric(
    id="ui_tick_p95_ms",
    name="UI Tick P95 Latency",
    category=MetricCategory.UI_PERFORMANCE,
    description="UI tick 第 95 百分位延迟",
    unit="ms",
    minimum_target=5.0,  # 目标是 < 5ms，所以测量值应该小于这个值
    professional_target=2.0,
    measurement_func="播放期间统计 UI tick 耗时，取 P95",
    notes="测量值越低越好，评估时反向判断",
))

METRICS.add_metric(AcceptanceMetric(
    id="ui_tick_max_ms",
    name="UI Tick Max Latency",
    category=MetricCategory.UI_PERFORMANCE,
    description="UI tick 最大延迟",
    unit="ms",
    minimum_target=20.0,
    professional_target=10.0,
    measurement_func="播放期间统计 UI tick 最大耗时",
    notes="测量值越低越好",
))

# 渲染器指标
METRICS.add_metric(AcceptanceMetric(
    id="renderer_queue_depth_p95",
    name="Renderer Queue Depth P95",
    category=MetricCategory.RENDERER,
    description="渲染器队列深度第 95 百分位",
    unit="frames",
    minimum_target=3.0,
    professional_target=2.0,
    measurement_func="播放期间统计渲染器队列深度，取 P95",
    notes="测量值越低越好，评估时反向判断",
))

METRICS.add_metric(AcceptanceMetric(
    id="renderer_queue_max_depth",
    name="Renderer Queue Max Depth",
    category=MetricCategory.RENDERER,
    description="渲染器队列最大深度",
    unit="frames",
    minimum_target=5.0,
    professional_target=3.0,
    measurement_func="播放期间统计渲染器队列最大深度",
    notes="测量值越低越好",
))

# 延迟指标
METRICS.add_metric(AcceptanceMetric(
    id="playback_latency_growth_rate",
    name="Playback Latency Growth Rate",
    category=MetricCategory.LATENCY,
    description="播放延迟增长速率",
    unit="ms/s",  # 每秒增加的延迟
    minimum_target=1.0,  # 每秒最多增加 1ms
    professional_target=0.0,  # 不增长
    measurement_func="播放 30 秒，统计延迟随时间的增长趋势",
    notes="最低目标：不持续增长；专业目标：始终低延迟",
))

METRICS.add_metric(AcceptanceMetric(
    id="seek_latency_p95_ms",
    name="Seek Latency P95",
    category=MetricCategory.LATENCY,
    description="Seek 操作延迟第 95 百分位",
    unit="ms",
    minimum_target=100.0,
    professional_target=50.0,
    measurement_func="播放期间执行多次 seek，统计 P95 延迟",
    notes="测量值越低越好",
))

# 交互指标
METRICS.add_metric(AcceptanceMetric(
    id="zoom_pan_render_storm_count",
    name="Zoom/Pan Render Storm Count",
    category=MetricCategory.INTERACTION,
    description="Zoom/Pan 期间的 full render storm 计数",
    unit="count",
    minimum_target=0.0,  # 不允许任何 render storm
    professional_target=0.0,
    measurement_func="播放期间执行 zoom/pan，统计 full render 触发次数",
    notes="最低目标：无 full render storm；专业目标：丝滑",
))

METRICS.add_metric(AcceptanceMetric(
    id="zoom_pan_response_ms",
    name="Zoom/Pan Response Time",
    category=MetricCategory.INTERACTION,
    description="Zoom/Pan 操作响应时间",
    unit="ms",
    minimum_target=50.0,
    professional_target=16.0,
    measurement_func="播放期间执行 zoom/pan，统计响应时间",
    notes="专业目标：丝滑（< 16ms）",
))

# Fallback 指标
METRICS.add_metric(AcceptanceMetric(
    id="fallback_v2_count",
    name="Fallback V2 Count",
    category=MetricCategory.FALLBACK,
    description="Fallback 到 v2 管线的次数",
    unit="count",
    minimum_target=1.0,  # 只在明确失败时 fallback
    professional_target=0.0,
    measurement_func="记录整个测试期间的 fallback 事件",
    notes="最低目标：只在明确失败时；专业目标：几乎不发生",
))

METRICS.add_metric(AcceptanceMetric(
    id="fallback_rate",
    name="Fallback Rate",
    category=MetricCategory.FALLBACK,
    description="Fallback 发生率",
    unit="rate",
    minimum_target=0.05,  # 最多 5% 的播放场景发生 fallback
    professional_target=0.0,
    measurement_func="Fallback 次数 / 总播放次数",
    notes="测量值越低越好",
))


def evaluate_inverse_metric(measured_value: float, metric: AcceptanceMetric) -> AcceptanceLevel:
    """
    评估反向指标（测量值越低越好）。

    对于 UI tick、队列深度等指标，测量值越低越好。

    Args:
        measured_value: 实际测量值
        metric: 指标定义

    Returns:
        AcceptanceLevel: 验收结果级别
    """
    # 反向评估：测量值 <= 目标值才算通过
    if measured_value <= metric.professional_target:
        return AcceptanceLevel.PROFESSIONAL
    elif measured_value <= metric.minimum_target:
        return AcceptanceLevel.MINIMUM
    else:
        return AcceptanceLevel.FAILED


def create_result_from_measurement(
    metric_id: str,
    measured_value: float,
    details: Optional[Dict[str, Any]] = None,
    message: str = "",
    test_duration_ms: float = 0.0,
    inverse: bool = False,
) -> AcceptanceResult:
    """
    从测量值创建验收结果。

    Args:
        metric_id: 指标 ID
        measured_value: 测量值
        details: 详细数据
        message: 结果描述
        test_duration_ms: 测试耗时
        inverse: 是否为反向指标（越低越好）

    Returns:
        AcceptanceResult: 验收结果
    """
    metric = METRICS.get_metric(metric_id)
    if not metric:
        raise ValueError(f"Unknown metric: {metric_id}")

    # 评估级别
    if inverse:
        level = evaluate_inverse_metric(measured_value, metric)
    else:
        level = metric.evaluate(measured_value)

    return AcceptanceResult(
        metric_id=metric_id,
        measured_value=measured_value,
        level=level,
        details=details or {},
        message=message,
        test_duration_ms=test_duration_ms,
    )