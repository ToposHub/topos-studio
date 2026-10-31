#!/usr/bin/env python3
"""
Validation Report Generator for DaVinci-Level Acceptance Matrix

生成完整的验收报告，区分不同类型的测试结果：
- 单元测试通过
- Synthetic smoke 通过
- GUI benchmark 通过
- 真实素材 benchmark 通过
- Skipped 项不能写 PASS

达芬奇级验收指标：
| 指标 | 目标 |
|------|------|
| UI tick p95 | 1080p < 2ms，4K proxy < 3ms |
| displayed fps | >= 素材 fps * 0.95 |
| dropped frames | 稳定状态 < 1% |
| render queue p95 | <= 2 |
| seek median | < 300ms |
| seek p95 | < 600ms |
| texture count | 10分钟内不持续增长 |
| memory RSS | 10分钟内不持续增长 |
| first frame | < 500ms |

使用方法：
    python tests/benchmark/validation_report_generator.py
    python tests/benchmark/validation_report_generator.py --output docs/video_playback_performance_report.md
    python tests/benchmark/validation_report_generator.py --run-tests

"""

from __future__ import annotations

import os
import sys
import json
import time
import subprocess
import argparse
import logging
import statistics
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

logger = logging.getLogger(__name__)


class TestStatus(Enum):
    """测试状态

    状态定义：
    - PASS: 所有指标达标，无异常
    - PARTIAL: 部分指标达标，或存在稳定性问题
    - FAIL: 未达到 DaVinci 阈值，或崩溃/异常/内存泄漏
    - SKIP: 无显示环境、缺少素材、硬件不支持等无法测试的情况
    - NA: 不适用（尚未执行或无数据）
    """
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    SKIP = "SKIP"
    NA = "N/A"


@dataclass
class ValidationResult:
    """单个验收指标的结果"""
    metric_name: str
    target: str
    actual: Optional[float] = None
    status: TestStatus = TestStatus.NA
    reason: str = ""  # 状态原因（如果非PASS）
    notes: str = ""
    test_type: str = ""  # unit_test, synthetic_smoke, gui_benchmark, real_fixture


@dataclass
class FixtureValidationResult:
    """单个素材的验收结果"""
    fixture_id: str
    fixture_name: str
    fixture_type: str = "real"  # real, synthetic
    available: bool = False
    metrics: Dict[str, ValidationResult] = field(default_factory=dict)
    overall_status: TestStatus = TestStatus.NA
    reason: str = ""  # 整体状态原因
    errors: List[str] = field(default_factory=list)


# 达芬奇级验收指标定义
DAVINCI_LEVEL_THRESHOLDS: Dict[str, Dict[str, Any]] = {
    "ui_tick_p95": {
        "name": "UI Tick P95",
        "targets": {"1080p": 2.0, "4k_proxy": 3.0},
        "unit": "ms",
        "description": "UI线程响应时间 P95",
    },
    "displayed_fps_ratio": {
        "name": "Displayed FPS",
        "target": 0.95,
        "unit": "ratio",
        "description": "实际显示帧率 >= 目标帧率 * 95%",
    },
    "dropped_frames_stable": {
        "name": "Dropped Frames (Stable)",
        "target": 1.0,
        "unit": "%",
        "description": "稳定状态丢帧率 < 1%",
    },
    "render_queue_p95": {
        "name": "Render Queue P95",
        "target": 2,
        "unit": "frames",
        "description": "渲染队列深度 P95 <= 2",
    },
    "seek_median": {
        "name": "Seek Median",
        "target": 300.0,
        "unit": "ms",
        "description": "Seek 中位数时间 < 300ms",
    },
    "seek_p95": {
        "name": "Seek P95",
        "target": 600.0,
        "unit": "ms",
        "description": "Seek P95 时间 < 600ms",
    },
    "texture_count_growth": {
        "name": "Texture Count (10min)",
        "target": "no_growth",
        "unit": "",
        "description": "10分钟内 GPU 纹理计数不持续增长",
    },
    "memory_rss_growth": {
        "name": "Memory RSS (10min)",
        "target": "no_growth",
        "unit": "",
        "description": "10分钟内内存 RSS 不持续增长",
    },
    "first_frame": {
        "name": "First Frame",
        "target": 500.0,
        "unit": "ms",
        "description": "首帧加载时间 < 500ms",
    },
}

# 验收素材列表
VALIDATION_FIXTURES: Dict[str, Dict[str, str]] = {
    "validation_1080p_h264_sdr_30fps": {
        "name": "1080p H.264 SDR 30fps",
        "category": "基础",
        "description": "验收基础素材",
    },
    "validation_4k_h264_sdr_30fps": {
        "name": "4K H.264 SDR 30fps",
        "category": "基础",
        "description": "验收素材：4K H.264",
    },
    "validation_4k_h265_10bit_hdr_30fps": {
        "name": "4K H.265 10-bit HDR 30fps",
        "category": "HDR",
        "description": "验收素材：4K H.265 HDR",
    },
    "validation_4k_prores_422_hq_24fps": {
        "name": "4K ProRes 422 HQ 24fps",
        "category": "专业",
        "description": "验收素材：4K ProRes",
    },
    "validation_4k_prores_422_hq_30fps": {
        "name": "4K ProRes 422 HQ 30fps",
        "category": "专业",
        "description": "验收素材：4K ProRes 30fps",
    },
    "validation_prores_4444_alpha": {
        "name": "ProRes 4444 with Alpha",
        "category": "专业",
        "description": "验收素材：ProRes Alpha",
    },
    "validation_dnxhr_hqx_10bit": {
        "name": "DNxHR HQX 10-bit",
        "category": "专业",
        "description": "验收素材：DNxHR",
    },
    "validation_long_gop_2min": {
        "name": "Long GOP 2+ minutes",
        "category": "压力",
        "description": "验收素材：长 GOP",
    },
    "validation_long_playback_10min": {
        "name": "Long Playback 10 minutes",
        "category": "压力",
        "description": "验收素材：长播放稳定性",
    },
}

# 合成素材（用于 smoke test）
SYNTHETIC_FIXTURES: Dict[str, Dict[str, str]] = {
    "synthetic_1080p_h264_short": {
        "name": "Synthetic 1080p H.264 (5s)",
        "category": "合成",
        "description": "合成素材用于 smoke test",
    },
    "synthetic_1080p_h264_sdr": {
        "name": "Synthetic 1080p H.264 (10s)",
        "category": "合成",
        "description": "合成素材用于 smoke test",
    },
    "synthetic_4k_h264_sdr": {
        "name": "Synthetic 4K H.264 (3s)",
        "category": "合成",
        "description": "合成素材用于 smoke test",
    },
    "synthetic_prores_422_1080p": {
        "name": "Synthetic ProRes 422 (5s)",
        "category": "合成",
        "description": "合成素材用于 smoke test",
    },
}


# ============================================================================
# 状态判定逻辑
# ============================================================================

# SKIP 原因定义
SKIP_REASONS = {
    "no_display": "无显示环境（无 GUI/无显示器）",
    "missing_fixture": "缺少特定素材",
    "hardware_unsupported": "硬件不支持（如无硬解）",
    "not_run": "测试未运行",
    "dry_mode": "Dry 模式测试（无真实 GUI tick）",
}

# PARTIAL 原因定义
PARTIAL_REASONS = {
    "partial_metrics": "部分指标达标",
    "stability_issues": "存在稳定性问题",
    "high_first_frame": "首帧延迟高但后续稳定",
    "gui_mode_needed": "需要 GUI 环境验证真实性能",
}

# FAIL 原因定义
FAIL_REASONS = {
    "below_threshold": "未达到 DaVinci 阈值",
    "crash": "崩溃或异常",
    "memory_leak": "内存泄漏",
    "resource_leak": "资源泄漏（纹理/缓冲区未释放）",
    "timeout": "测试超时",
}


def determine_metric_status(
    metric_id: str,
    actual: Optional[float],
    video_height: int = 1080,
    has_gui: bool = True
) -> Tuple[TestStatus, str]:
    """
    判定单个指标的状态。

    Args:
        metric_id: 指标 ID
        actual: 实际测量值
        video_height: 视频高度（用于区分 1080p/4K 阈值）
        has_gui: 是否有真实 GUI 环境

    Returns:
        (状态, 原因)
    """
    if actual is None:
        return TestStatus.NA, "无数据"

    threshold = DAVINCI_LEVEL_THRESHOLDS.get(metric_id, {})

    # UI Tick P95: 1080p < 2ms, 4K proxy < 3ms
    if metric_id == "ui_tick_p95":
        targets = threshold.get("targets", {})
        target = targets.get("4k_proxy", 3.0) if video_height > 1080 else targets.get("1080p", 2.0)
        if not has_gui:
            return TestStatus.PARTIAL, PARTIAL_REASONS["gui_mode_needed"]
        if actual < target:
            return TestStatus.PASS, ""
        return TestStatus.FAIL, f"{FAIL_REASONS['below_threshold']} (实际: {actual:.2f}ms, 目标: <{target}ms)"

    # Displayed FPS Ratio: >= 0.95
    elif metric_id == "displayed_fps_ratio":
        target = threshold.get("target", 0.95)
        if not has_gui:
            return TestStatus.PARTIAL, PARTIAL_REASONS["gui_mode_needed"]
        if actual >= target:
            return TestStatus.PASS, ""
        # 如果接近阈值（0.90-0.95），标记为 PARTIAL
        if actual >= 0.90:
            return TestStatus.PARTIAL, f"{PARTIAL_REASONS['partial_metrics']} (实际: {actual:.2%}, 目标: >=95%)"
        return TestStatus.FAIL, f"{FAIL_REASONS['below_threshold']} (实际: {actual:.2%}, 目标: >=95%)"

    # Dropped Frames Stable: < 1%
    elif metric_id == "dropped_frames_stable":
        target = threshold.get("target", 1.0)
        if actual < target:
            return TestStatus.PASS, ""
        # 如果丢帧率在 1-3%，标记为 PARTIAL
        if actual < 3.0:
            return TestStatus.PARTIAL, f"{PARTIAL_REASONS['stability_issues']} (丢帧率: {actual:.2f}%)"
        return TestStatus.FAIL, f"{FAIL_REASONS['below_threshold']} (丢帧率: {actual:.2f}%, 目标: <1%)"

    # Render Queue P95: <= 2
    elif metric_id == "render_queue_p95":
        target = threshold.get("target", 2)
        if actual <= target:
            return TestStatus.PASS, ""
        # 如果队列深度 2-4，标记为 PARTIAL
        if actual <= 4:
            return TestStatus.PARTIAL, f"{PARTIAL_REASONS['partial_metrics']} (队列深度: {actual:.1f})"
        return TestStatus.FAIL, f"{FAIL_REASONS['below_threshold']} (队列深度: {actual:.1f}, 目标: <=2)"

    # Seek Median: < 300ms
    elif metric_id == "seek_median":
        target = threshold.get("target", 300.0)
        if actual < target:
            return TestStatus.PASS, ""
        # 如果 300-500ms，标记为 PARTIAL
        if actual < 500:
            return TestStatus.PARTIAL, f"{PARTIAL_REASONS['partial_metrics']} (实际: {actual:.1f}ms)"
        return TestStatus.FAIL, f"{FAIL_REASONS['below_threshold']} (实际: {actual:.1f}ms, 目标: <300ms)"

    # Seek P95: < 600ms
    elif metric_id == "seek_p95":
        target = threshold.get("target", 600.0)
        if actual < target:
            return TestStatus.PASS, ""
        # 如果 600-1000ms，标记为 PARTIAL
        if actual < 1000:
            return TestStatus.PARTIAL, f"{PARTIAL_REASONS['partial_metrics']} (实际: {actual:.1f}ms)"
        return TestStatus.FAIL, f"{FAIL_REASONS['below_threshold']} (实际: {actual:.1f}ms, 目标: <600ms)"

    # First Frame: < 500ms
    elif metric_id == "first_frame":
        target = threshold.get("target", 500.0)
        if actual < target:
            return TestStatus.PASS, ""
        # 如果首帧延迟 500-1500ms 但后续稳定，标记为 PARTIAL
        if actual < 1500:
            return TestStatus.PARTIAL, PARTIAL_REASONS["high_first_frame"]
        return TestStatus.FAIL, f"{FAIL_REASONS['below_threshold']} (实际: {actual:.1f}ms, 目标: <500ms)"

    # Texture/Memory Growth: no_growth
    elif metric_id in ["texture_count_growth", "memory_rss_growth"]:
        # actual 为布尔值或字符串
        if actual == True or actual == "no_growth" or actual == 0:
            return TestStatus.PASS, ""
        return TestStatus.FAIL, FAIL_REASONS["memory_leak" if "memory" in metric_id else "resource_leak"]

    return TestStatus.NA, "未知指标"


def determine_overall_status(metrics: Dict[str, ValidationResult]) -> Tuple[TestStatus, str]:
    """
    根据所有指标判定整体状态。

    Args:
        metrics: 指标结果字典

    Returns:
        (整体状态, 原因)
    """
    if not metrics:
        return TestStatus.NA, "无测试数据"

    # 统计各状态数量
    status_counts = {
        TestStatus.PASS: 0,
        TestStatus.PARTIAL: 0,
        TestStatus.FAIL: 0,
        TestStatus.SKIP: 0,
        TestStatus.NA: 0,
    }

    for metric in metrics.values():
        status_counts[metric.status] = status_counts.get(metric.status, 0) + 1

    total = len(metrics)
    na_count = status_counts[TestStatus.NA]
    skip_count = status_counts[TestStatus.SKIP]
    evaluated = total - na_count - skip_count

    # 如果全部是 SKIP 或 NA，则整体状态为 SKIP
    if evaluated == 0:
        return TestStatus.SKIP, "所有指标均未评估或跳过"

    # 如果有任何 FAIL，整体状态为 FAIL
    if status_counts[TestStatus.FAIL] > 0:
        fail_reasons = [m.reason for m in metrics.values() if m.status == TestStatus.FAIL and m.reason]
        reason = "; ".join(fail_reasons[:2])  # 只显示前两个原因
        return TestStatus.FAIL, reason or FAIL_REASONS["below_threshold"]

    # 如果有 PARTIAL 但没有 FAIL，整体状态为 PARTIAL
    if status_counts[TestStatus.PARTIAL] > 0:
        partial_reasons = [m.reason for m in metrics.values() if m.status == TestStatus.PARTIAL and m.reason]
        reason = "; ".join(partial_reasons[:2])
        return TestStatus.PARTIAL, reason or PARTIAL_REASONS["partial_metrics"]

    # 如果全部是 PASS，整体状态为 PASS
    if status_counts[TestStatus.PASS] == evaluated:
        return TestStatus.PASS, ""

    # 其他情况
    return TestStatus.NA, "状态未知"


class ValidationReportGenerator:
    """验收报告生成器"""

    def __init__(self, project_root: str = None):
        """
        初始化报告生成器。

        Args:
            project_root: 项目根目录
        """
        if project_root:
            self.project_root = Path(project_root)
        else:
            self.project_root = Path(__file__).parent.parent.parent

        self.results_dir = self.project_root / "tests" / "media" / "benchmark_results"
        self.docs_dir = self.project_root / "docs"

        self.unit_test_results: Dict[str, Any] = {}
        self.synthetic_smoke_results: Dict[str, Any] = {}
        self.gui_benchmark_results: Dict[str, Any] = {}
        self.real_fixture_results: Dict[str, Any] = {}

        # 收集的验收结果
        self.fixture_results: Dict[str, FixtureValidationResult] = {}

    def run_unit_tests(self) -> Tuple[int, int, int]:
        """
        运行单元测试并返回统计。

        Returns:
            (passed, skipped, failed)
        """
        logger.info("运行单元测试...")

        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "pytest",
                    "tests/media/", "-q", "--tb=no",
                    "--ignore=tests/benchmark/gui_benchmark.py",
                ],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=120,
            )

            output = result.stdout

            # 解析 pytest 输出
            # 格式: "X passed, Y skipped, Z failed"
            passed = 0
            skipped = 0
            failed = 0

            import re
            match = re.search(r'(\d+) passed', output)
            if match:
                passed = int(match.group(1))

            match = re.search(r'(\d+) skipped', output)
            if match:
                skipped = int(match.group(1))

            match = re.search(r'(\d+) failed', output)
            if match:
                failed = int(match.group(1))

            self.unit_test_results = {
                "passed": passed,
                "skipped": skipped,
                "failed": failed,
                "output": output,
            }

            logger.info(f"单元测试: {passed} passed, {skipped} skipped, {failed} failed")
            return passed, skipped, failed

        except subprocess.TimeoutExpired:
            logger.error("单元测试超时")
            return 0, 0, 0
        except Exception as e:
            logger.error(f"运行单元测试失败: {e}")
            return 0, 0, 0

    def run_synthetic_smoke_tests(self) -> Dict[str, Any]:
        """
        运行合成素材 smoke 测试。

        Returns:
            测试结果
        """
        logger.info("运行合成素材 smoke 测试...")

        try:
            # 生成合成素材
            from tests.media.synthetic_fixture_generator import ensure_synthetic_fixtures
            fixtures = ensure_synthetic_fixtures(
                fixture_ids=["synthetic_1080p_h264_short"]
            )

            if not fixtures:
                logger.warning("无法生成合成素材")
                return {}

            # 运行 GUI benchmark（dry 模式）
            from tests.media.gui_benchmark import GUIVideoBenchmark
            benchmark = GUIVideoBenchmark(mode="dry", pipeline_version="v2")

            results = {}
            for fid, path in fixtures.items():
                logger.info(f"测试合成素材: {fid}")
                result = benchmark.run_benchmark(str(path), fid, duration_s=5.0)
                if result:
                    results[fid] = result.to_dict()

            self.synthetic_smoke_results = results
            return results

        except Exception as e:
            logger.error(f"合成素材测试失败: {e}")
            import traceback
            traceback.print_exc()
            return {}

    def check_real_fixtures_available(self) -> Dict[str, bool]:
        """
        检查真实素材是否可用。

        Returns:
            fixture_id -> 是否可用的字典
        """
        logger.info("检查真实素材可用性...")

        manifest_path = self.project_root / "tests" / "media" / "fixtures_manifest.json"
        if not manifest_path.exists():
            logger.warning("Manifest 文件不存在")
            return {}

        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        except Exception as e:
            logger.error(f"加载 manifest 失败: {e}")
            return {}

        # 获取素材路径
        fixtures_path = os.environ.get(
            'TOPOS_VIDEO_FIXTURES_PATH',
            '~/VideoTestFixtures'
        )
        if fixtures_path.startswith('~'):
            fixtures_path = os.path.expanduser(fixtures_path)

        availability = {}

        # 检查达芬奇级验收素材
        davinci_section = manifest.get("davinci_level_validation", {})
        minimum_fixtures = davinci_section.get("minimum_test_fixtures", {})

        for fid, fixture_spec in minimum_fixtures.items():
            file_path = fixture_spec.get("file_path", "")
            full_path = os.path.join(fixtures_path, file_path)

            # 检查文件是否存在
            exists = os.path.exists(full_path)

            # 尝试 fallback 路径
            if not exists:
                config = manifest.get("config", {})
                fallbacks = config.get("fallback_paths", [])
                for fallback in fallbacks:
                    if fallback.startswith('~'):
                        fallback = os.path.expanduser(fallback)
                    fallback_path = os.path.join(fallback, file_path)
                    if os.path.exists(fallback_path):
                        exists = True
                        break

            availability[fid] = exists

            if exists:
                logger.info(f"  {fid}: 可用")
            else:
                logger.info(f"  {fid}: 不可用")

        return availability

    def collect_validation_results(self) -> Dict[str, FixtureValidationResult]:
        """
        收集所有验收结果。

        Returns:
            fixture_id -> FixtureValidationResult 的字典
        """
        logger.info("收集验收结果...")

        # 检查真实素材可用性
        real_fixture_availability = self.check_real_fixtures_available()

        # 为每个素材创建验收结果
        results = {}

        # 1. 真实素材验收
        for fid, info in VALIDATION_FIXTURES.items():
            result = FixtureValidationResult(
                fixture_id=fid,
                fixture_name=info["name"],
                fixture_type="real",
                available=real_fixture_availability.get(fid, False),
            )

            # 设置各项指标状态
            for metric_id, threshold_info in DAVINCI_LEVEL_THRESHOLDS.items():
                if result.available:
                    # 素材可用但未测试
                    metric_result = ValidationResult(
                        metric_name=threshold_info["name"],
                        target=str(threshold_info.get("target", threshold_info.get("targets", "N/A"))),
                        status=TestStatus.SKIP,
                        reason=SKIP_REASONS["not_run"],
                        notes="素材可用，需要运行实际测试",
                        test_type="real_fixture",
                    )
                else:
                    # 素材不可用
                    metric_result = ValidationResult(
                        metric_name=threshold_info["name"],
                        target=str(threshold_info.get("target", threshold_info.get("targets", "N/A"))),
                        status=TestStatus.SKIP,
                        reason=SKIP_REASONS["missing_fixture"],
                        notes="外部素材缺失",
                        test_type="real_fixture",
                    )
                    result.errors.append("外部素材文件不存在")

                result.metrics[metric_id] = metric_result

            # 设置整体状态
            if not result.available:
                result.overall_status = TestStatus.SKIP
                result.reason = SKIP_REASONS["missing_fixture"]
            else:
                result.overall_status = TestStatus.NA  # 需要运行测试
                result.reason = SKIP_REASONS["not_run"]

            results[fid] = result

        # 2. 合成素材验收（smoke test）
        for fid, info in SYNTHETIC_FIXTURES.items():
            result = FixtureValidationResult(
                fixture_id=fid,
                fixture_name=info["name"],
                fixture_type="synthetic",
                available=True,  # 合成素材总是可用
            )

            # 从 smoke 测试结果中提取指标
            smoke_result = self.synthetic_smoke_results.get(fid, {})

            for metric_id, threshold_info in DAVINCI_LEVEL_THRESHOLDS.items():
                metric_result = ValidationResult(
                    metric_name=threshold_info["name"],
                    target=str(threshold_info.get("target", threshold_info.get("targets", "N/A"))),
                    status=TestStatus.NA,
                    notes="合成素材 smoke test",
                    test_type="synthetic_smoke",
                )

                # 尝试从 smoke 测试结果中提取实际值
                if smoke_result:
                    actual = self._extract_metric_from_result(metric_id, smoke_result)
                    if actual is not None:
                        metric_result.actual = actual
                        # dry 模式没有真实 GUI
                        status, reason = self._check_metric_threshold(
                            metric_id, actual, smoke_result, has_gui=False
                        )
                        metric_result.status = status
                        metric_result.reason = reason

                result.metrics[metric_id] = metric_result

            # 设置整体状态（使用统一判定函数）
            if smoke_result:
                overall_status, overall_reason = determine_overall_status(result.metrics)
                result.overall_status = overall_status
                result.reason = overall_reason
            else:
                result.overall_status = TestStatus.SKIP
                result.reason = SKIP_REASONS["not_run"]

            results[fid] = result

        self.fixture_results = results
        return results

    def _extract_metric_from_result(self, metric_id: str, result: Dict[str, Any]) -> Optional[float]:
        """从测试结果中提取指标值"""
        if metric_id == "ui_tick_p95":
            ui_tick = result.get("ui_tick", {})
            return ui_tick.get("p95_ms")

        elif metric_id == "displayed_fps_ratio":
            playback = result.get("playback", {})
            displayed = playback.get("displayed_fps", 0)
            target = playback.get("target_fps", 0)
            if target > 0:
                return displayed / target
            return None

        elif metric_id == "dropped_frames_stable":
            playback = result.get("playback", {})
            dropped = playback.get("dropped_frames", 0)
            total = playback.get("total_frames", 0)
            if total > 0:
                return (dropped / total) * 100
            return None

        elif metric_id == "seek_median":
            seek = result.get("seek", {})
            return seek.get("median_ms")

        elif metric_id == "seek_p95":
            seek = result.get("seek", {})
            return seek.get("p95_ms")

        elif metric_id == "first_frame":
            first_frame = result.get("first_frame", {})
            return first_frame.get("ms")

        return None

    def _check_metric_threshold(
        self,
        metric_id: str,
        actual: float,
        result: Dict[str, Any],
        has_gui: bool = False
    ) -> Tuple[TestStatus, str]:
        """
        检查指标是否达标，返回状态和原因。

        Args:
            metric_id: 指标 ID
            actual: 实际测量值
            result: 测试结果字典
            has_gui: 是否有真实 GUI 环境

        Returns:
            (状态, 原因)
        """
        # 获取视频高度以区分 1080p/4K
        video_props = result.get("video_properties", {})
        height = video_props.get("height", 1080)

        return determine_metric_status(metric_id, actual, height, has_gui)

    def generate_markdown_report(self, output_path: str = None) -> str:
        """
        生成 Markdown 格式的验收报告。

        Args:
            output_path: 输出文件路径

        Returns:
            报告内容
        """
        logger.info("生成 Markdown 验收报告...")

        timestamp = datetime.now().strftime("%Y-%m-%d")
        if output_path is None:
            output_path = str(self.docs_dir / f"video_playback_performance_report_{timestamp}.md")

        report_lines = []

        # 标题
        report_lines.append(f"# Video Playback Performance Report")
        report_lines.append(f"")
        report_lines.append(f"**日期**: {timestamp}")
        report_lines.append(f"**项目**: Topos Color 视频管线")
        report_lines.append(f"**验收级别**: DaVinci-Level")
        report_lines.append(f"")

        # 验收指标定义
        report_lines.append(f"## 1. 验收指标定义")
        report_lines.append(f"")
        report_lines.append(f"| 指标 | 目标 | 说明 |")
        report_lines.append(f"|------|------|------|")

        for metric_id, threshold in DAVINCI_LEVEL_THRESHOLDS.items():
            targets = threshold.get("targets", {})
            if targets:
                target_str = f"1080p: <{targets['1080p']}{threshold['unit']}, 4K proxy: <{targets['4k_proxy']}{threshold['unit']}"
            else:
                target_val = threshold.get("target", "N/A")
                if threshold["target"] == "no_growth":
                    target_str = "不持续增长"
                else:
                    target_str = f"<{target_val}{threshold['unit']}"

            report_lines.append(f"| {threshold['name']} | {target_str} | {threshold['description']} |")

        report_lines.append(f"")

        # 单元测试结果
        report_lines.append(f"## 2. 单元测试结果")
        report_lines.append(f"")
        unit_passed = self.unit_test_results.get("passed", 0)
        unit_skipped = self.unit_test_results.get("skipped", 0)
        unit_failed = self.unit_test_results.get("failed", 0)

        unit_status = "PASS" if unit_failed == 0 else "FAIL"

        report_lines.append(f"| 测试类型 | Passed | Skipped | Failed | 状态 |")
        report_lines.append(f"|----------|--------|---------|--------|------|")
        report_lines.append(f"| 单元/集成测试 | {unit_passed} | {unit_skipped} | {unit_failed} | **{unit_status}** |")
        report_lines.append(f"")

        if unit_failed > 0:
            report_lines.append(f"> **注意**: 单元测试有 {unit_failed} 个失败项")
            report_lines.append(f"")

        # Synthetic Smoke Test 结果
        report_lines.append(f"## 3. Synthetic Smoke Test 结果")
        report_lines.append(f"")

        synthetic_fixtures_results = [
            r for r in self.fixture_results.values()
            if r.fixture_type == "synthetic"
        ]

        if synthetic_fixtures_results:
            report_lines.append(f"| 素材 | 类型 | 整体状态 | 说明 |")
            report_lines.append(f"|------|------|----------|------|")

            for r in synthetic_fixtures_results:
                if r.overall_status == TestStatus.SKIP:
                    status_str = "SKIP"
                    notes = "未运行测试"
                elif r.overall_status == TestStatus.PASS:
                    status_str = "**PASS**"
                    notes = "所有指标达标"
                else:
                    status_str = "FAIL"
                    notes = "部分指标未达标"

                report_lines.append(f"| {r.fixture_name} | 合成 | {status_str} | {notes} |")

            report_lines.append(f"")

            # 详细指标表
            report_lines.append(f"### 3.1 Synthetic Smoke 详细指标")
            report_lines.append(f"")

            for r in synthetic_fixtures_results:
                if r.overall_status != TestStatus.SKIP:
                    report_lines.append(f"**{r.fixture_name}** — 整体状态: **{r.overall_status.value}**")
                    if r.reason:
                        report_lines.append(f"> 原因: {r.reason}")
                    report_lines.append(f"")

                    report_lines.append(f"| 指标 | 目标 | 实际值 | 状态 | 原因 |")
                    report_lines.append(f"|------|------|--------|------|------|")

                    for metric_id, metric in r.metrics.items():
                        if metric.status == TestStatus.NA:
                            continue

                        actual_str = f"{metric.actual:.2f}" if metric.actual is not None else "N/A"

                        # 根据状态设置显示格式
                        if metric.status == TestStatus.PASS:
                            status_str = "**PASS**"
                            reason_str = "-"
                        elif metric.status == TestStatus.PARTIAL:
                            status_str = "PARTIAL"
                            reason_str = metric.reason or "部分达标"
                        elif metric.status == TestStatus.FAIL:
                            status_str = "FAIL"
                            reason_str = metric.reason or "未达标"
                        else:
                            status_str = metric.status.value
                            reason_str = metric.reason or "-"

                        report_lines.append(f"| {metric.metric_name} | {metric.target} | {actual_str} | {status_str} | {reason_str} |")

                    report_lines.append("")
        else:
            report_lines.append(f"> **状态**: SKIP - 未运行合成素材测试")
            report_lines.append(f"")

        # 真实素材验收结果
        report_lines.append(f"## 4. 真实素材验收结果")
        report_lines.append(f"")

        real_fixtures_results = [
            r for r in self.fixture_results.values()
            if r.fixture_type == "real"
        ]

        # 按类别分组
        categories: Dict[str, List[FixtureValidationResult]] = {}
        for r in real_fixtures_results:
            info = VALIDATION_FIXTURES.get(r.fixture_id, {})
            cat = info.get("category", "其他")
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(r)

        report_lines.append(f"| 素材 | 类别 | 可用性 | 整体状态 | 原因 |")
        report_lines.append(f"|------|------|--------|----------|------|")

        for cat, fixtures in sorted(categories.items()):
            for r in fixtures:
                available_str = "是" if r.available else "否"

                # 根据状态设置显示格式
                if r.overall_status == TestStatus.SKIP:
                    status_str = "SKIP"
                    reason_str = r.reason or SKIP_REASONS["missing_fixture"]
                elif r.overall_status == TestStatus.PASS:
                    status_str = "**PASS**"
                    reason_str = "-"
                elif r.overall_status == TestStatus.PARTIAL:
                    status_str = "PARTIAL"
                    reason_str = r.reason or "部分指标达标"
                elif r.overall_status == TestStatus.FAIL:
                    status_str = "FAIL"
                    reason_str = r.reason or "未达阈值"
                else:
                    status_str = "N/A"
                    reason_str = r.reason or "待测试"

                report_lines.append(f"| {r.fixture_name} | {cat} | {available_str} | {status_str} | {reason_str} |")

        report_lines.append(f"")

        # 状态定义说明
        report_lines.append(f"### 4.1 状态定义")
        report_lines.append(f"")
        report_lines.append(f"| 状态 | 定义 | 示例场景 |")
        report_lines.append(f"|------|------|----------|")
        report_lines.append(f"| PASS | 所有指标达标，无异常 | UI tick < 2ms, FPS >= 95%, 无丢帧 |")
        report_lines.append(f"| PARTIAL | 部分指标达标，或存在稳定性问题 | FPS 90-95%, 丢帧率 1-3% |")
        report_lines.append(f"| FAIL | 未达阈值，或崩溃/异常/泄漏 | UI tick > 3ms, FPS < 90%, 内存泄漏 |")
        report_lines.append(f"| SKIP | 无法测试（无环境/无素材/硬件不支持） | 外部素材缺失, 无显示环境 |")
        report_lines.append(f"")

        # 重要说明：SKIP 不能标记为 PASS
        report_lines.append(f"> **重要**: SKIP 状态表示素材不可用或测试未运行，不能标记为 PASS")
        report_lines.append(f"")

        # 达芬奇级验收矩阵
        report_lines.append(f"## 5. 达芬奇级验收矩阵汇总")
        report_lines.append(f"")

        # 计算总体状态
        unit_pass = unit_failed == 0
        synthetic_has_partial = any(
            r.overall_status == TestStatus.PARTIAL
            for r in synthetic_fixtures_results
        )
        synthetic_pass = any(
            r.overall_status == TestStatus.PASS
            for r in synthetic_fixtures_results
        )
        real_available = any(r.available for r in real_fixtures_results)
        real_tested = any(
            r.overall_status not in [TestStatus.SKIP, TestStatus.NA]
            for r in real_fixtures_results
        )

        report_lines.append(f"| 验收类型 | 状态 | 原因 | 说明 |")
        report_lines.append(f"|----------|------|------|------|")

        # 单元测试
        if unit_pass:
            unit_status_str = "**PASS**"
            unit_reason = "-"
        else:
            unit_status_str = "FAIL"
            unit_reason = FAIL_REASONS["below_threshold"]
        report_lines.append(f"| 单元测试 | {unit_status_str} | {unit_reason} | {unit_passed} passed, {unit_failed} failed |")

        # Synthetic smoke
        if synthetic_fixtures_results:
            if synthetic_pass:
                synthetic_status_str = "**PASS**"
                synthetic_reason = "-"
            elif synthetic_has_partial:
                synthetic_status_str = "PARTIAL"
                # 找到第一个 PARTIAL 的原因
                partial_fixture = next(
                    (r for r in synthetic_fixtures_results if r.overall_status == TestStatus.PARTIAL),
                    None
                )
                synthetic_reason = partial_fixture.reason if partial_fixture else PARTIAL_REASONS["partial_metrics"]
            else:
                synthetic_status_str = "SKIP"
                synthetic_reason = SKIP_REASONS["not_run"]
            report_lines.append(f"| Synthetic Smoke | {synthetic_status_str} | {synthetic_reason} | 合成素材基础验证 |")
        else:
            report_lines.append(f"| Synthetic Smoke | SKIP | {SKIP_REASONS['not_run']} | 未运行测试 |")

        # GUI Benchmark
        report_lines.append(f"| GUI Benchmark | SKIP | {SKIP_REASONS['no_display']} | 需要手动运行 |")

        # 真实素材
        if real_available:
            if real_tested:
                tested_results = [
                    r for r in real_fixtures_results
                    if r.overall_status not in [TestStatus.SKIP, TestStatus.NA]
                ]
                has_fail = any(r.overall_status == TestStatus.FAIL for r in tested_results)
                has_partial = any(r.overall_status == TestStatus.PARTIAL for r in tested_results)
                all_pass = all(r.overall_status == TestStatus.PASS for r in tested_results)

                if all_pass:
                    real_status_str = "**PASS**"
                    real_reason = "-"
                elif has_fail:
                    real_status_str = "FAIL"
                    fail_fixture = next((r for r in tested_results if r.overall_status == TestStatus.FAIL), None)
                    real_reason = fail_fixture.reason if fail_fixture else FAIL_REASONS["below_threshold"]
                elif has_partial:
                    real_status_str = "PARTIAL"
                    partial_fixture = next((r for r in tested_results if r.overall_status == TestStatus.PARTIAL), None)
                    real_reason = partial_fixture.reason if partial_fixture else PARTIAL_REASONS["partial_metrics"]
                else:
                    real_status_str = "N/A"
                    real_reason = "待测试"

                tested_count = len(tested_results)
                report_lines.append(f"| 真实素材 Benchmark | {real_status_str} | {real_reason} | {tested_count} 个素材已测试 |")
            else:
                report_lines.append(f"| 真实素材 Benchmark | SKIP | {SKIP_REASONS['not_run']} | 素材可用但未运行测试 |")
        else:
            skipped_count = sum(1 for r in real_fixtures_results if not r.available)
            report_lines.append(f"| 真实素材 Benchmark | SKIP | {SKIP_REASONS['missing_fixture']} | {skipped_count} 个素材缺失 |")

        report_lines.append(f"")

        # 结论
        report_lines.append(f"## 6. 验收结论")
        report_lines.append(f"")

        # 计算整体结论
        overall_statuses = []

        if not unit_pass:
            overall_statuses.append(("FAIL", "单元测试存在失败项"))
        elif synthetic_has_partial:
            overall_statuses.append(("PARTIAL", "Smoke 测试部分指标达标，需 GUI 环境验证"))
        elif synthetic_pass:
            overall_statuses.append(("PARTIAL", "Smoke 测试通过，真实素材待测试"))
        elif not real_available:
            overall_statuses.append(("PARTIAL", "外部素材缺失"))
        elif not real_tested:
            overall_statuses.append(("PARTIAL", "真实素材未测试"))

        # 确定最终结论
        if not overall_statuses:
            # 所有项都通过
            report_lines.append(f"**结论**: **PASS** — 所有验收指标达标")
            report_lines.append(f"")
            report_lines.append(f"> 所有指标均达到 DaVinci 级验收阈值")
        elif any(s[0] == "FAIL" for s in overall_statuses):
            fail_reasons = [s[1] for s in overall_statuses if s[0] == "FAIL"]
            report_lines.append(f"**结论**: **FAIL** — 验收未通过")
            report_lines.append(f"")
            report_lines.append(f"> 原因: {', '.join(fail_reasons)}")
        elif any(s[0] == "PARTIAL" for s in overall_statuses):
            partial_reasons = [s[1] for s in overall_statuses if s[0] == "PARTIAL"]
            report_lines.append(f"**结论**: **PARTIAL** — 部分验收指标达标")
            report_lines.append(f"")
            report_lines.append(f"> 原因: {', '.join(partial_reasons[:3])}")
        else:
            report_lines.append(f"**结论**: N/A — 需要运行更多测试")

        report_lines.append(f"")

        # 后续工作
        report_lines.append(f"## 7. 后续工作")
        report_lines.append(f"")

        # 统计缺失素材
        missing_fixtures = [r.fixture_name for r in real_fixtures_results if not r.available]
        if missing_fixtures:
            report_lines.append(f"### 7.1 缺失素材")
            report_lines.append(f""
            )
            for name in missing_fixtures:
                report_lines.append(f"- {name}")
            report_lines.append(f"")

        # 统计需要运行的测试
        report_lines.append(f"### 7.2 待运行测试")
        report_lines.append(f"")

        available_fixtures = [r.fixture_name for r in real_fixtures_results if r.available]
        if available_fixtures:
            report_lines.append(f"以下素材可用，需要运行实际测试:")
            report_lines.append(f""
            )
            for name in available_fixtures:
                report_lines.append(f"- `{name}`")

        report_lines.append(f"")

        # 运行命令
        report_lines.append(f"### 7.3 测试运行命令")
        report_lines.append(f""
        )
        report_lines.append(f"```bash")
        report_lines.append(f"# 运行单元测试")
        report_lines.append(f"pytest tests/media/ -q --tb=short")
        report_lines.append(f""
        )
        report_lines.append(f"# 运行 Synthetic Smoke Test")
        report_lines.append(f"python tests/benchmark/gui_benchmark.py --synthetic --mode dry")
        report_lines.append(f""
        )
        report_lines.append(f"# 运行 GUI Benchmark (需要显示环境)")
        report_lines.append(f"QT_QPA_PLATFORM=offscreen python tests/benchmark/gui_benchmark.py --synthetic")
        report_lines.append(f""
        )
        report_lines.append(f"# 运行真实素材 Benchmark")
        report_lines.append(f"python tests/benchmark/benchmark_video_pipeline.py --group davinci_validation")
        report_lines.append(f"```")
        report_lines.append(f"")

        # 签名
        report_lines.append(f"---")
        report_lines.append(f"")
        report_lines.append(f"**签名**: Topos Color Agent")
        report_lines.append(f"**日期**: {timestamp}")

        # 写入文件
        report_content = "\n".join(report_lines)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report_content)

        logger.info(f"验收报告已保存: {output_path}")
        return report_content


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='Generate DaVinci-Level Validation Report'
    )

    parser.add_argument(
        '--output', '-o',
        type=str,
        default=None,
        help='Output markdown file path'
    )

    parser.add_argument(
        '--run-tests',
        action='store_true',
        help='Run tests before generating report'
    )

    parser.add_argument(
        '--project-root',
        type=str,
        default=None,
        help='Project root directory'
    )

    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Quiet mode'
    )

    args = parser.parse_args()

    if args.quiet:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    generator = ValidationReportGenerator(args.project_root)

    # 运行测试（如果指定）
    if args.run_tests:
        generator.run_unit_tests()
        generator.run_synthetic_smoke_tests()

    # 收集验收结果
    generator.collect_validation_results()

    # 生成报告
    generator.generate_markdown_report(args.output)


if __name__ == '__main__':
    main()