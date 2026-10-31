"""
Final-7 (FINAL-P0-07): Realtime playback benchmark gate tests。

这些测试作为 benchmark suite 的回归 gate（不需要真实素材也能跑），
确保：
1. playback_benchmark.py 脚本可执行且输出符合预期格式。
2. source_decision 场景 realtime_master_requests=0、stale_generation=0、
   wrong_source_frame=0（核心 gate）。
3. seek_stress 场景 generation 不堆积 stale。
4. AVSyncController 决策分布合理。
5. gate check 正确识别违规。

不在 CI 里跑真实素材解码（耗时 + 依赖 PyAV），但保留 smoke test。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "tools" / "benchmarks" / "playback_benchmark.py"


# =============================================================================
# 1. benchmark 脚本可执行（source_decision + seek_stress + avsync，跳过 decode_real）
# =============================================================================


def test_benchmark_script_runs_and_passes(tmp_path):
    """benchmark 脚本必须可执行且 default gate 全绿。"""
    out = tmp_path / "bench.md"
    result = subprocess.run(
        [
            sys.executable,
            str(BENCH),
            "--frames", "20",
            "--scenario", "source_decision",
            "--out", str(out),
        ],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 0, f"benchmark 退出码非 0：\n{result.stderr}"
    assert out.exists(), "markdown 报告必须生成"
    json_out = out.with_suffix(".json")
    assert json_out.exists(), "JSON 报告必须生成"

    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["overall_pass"] is True
    assert data["failures"] == []
    assert len(data["scenarios"]) >= 1


def test_benchmark_seek_stress_no_stale_generation(tmp_path):
    """seek_stress 场景 stale_generation 必须 == 0。"""
    out = tmp_path / "seek.md"
    result = subprocess.run(
        [
            sys.executable,
            str(BENCH),
            "--frames", "20",
            "--scenario", "seek_stress",
            "--out", str(out),
        ],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 0
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    seek_scenarios = [s for s in data["scenarios"] if s["name"] == "seek_stress"]
    assert len(seek_scenarios) == 1
    s = seek_scenarios[0]
    assert s["stale_generation"] == 0, "seek stress 不应堆积 stale generation"
    assert s["wrong_source_frame"] == 0
    assert s["master_requests"] == 0


def test_benchmark_avsync_decision_distribution(tmp_path):
    """AVSync 场景必须产生非空决策分布。"""
    out = tmp_path / "avsync.md"
    result = subprocess.run(
        [
            sys.executable,
            str(BENCH),
            "--frames", "10",
            "--scenario", "avsync",
            "--out", str(out),
        ],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 0
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    stats = data.get("avsync_stats", {})
    assert stats.get("evaluations", 0) > 0
    # 正常 drift 序列应该以 IGNORE 为主（95% 时间 drift 0-1 帧）
    assert stats.get("ignores", 0) > 0


# =============================================================================
# 2. 直接调 scenario 函数验证 gate（更快，不需 subprocess）
# =============================================================================


def test_source_decision_scenario_realtime_no_master():
    """source_decision 场景 REALTIME 模式 master_requests 必须 0。"""
    sys.path.insert(0, str(ROOT))
    from tools.benchmarks.playback_benchmark import scenario_source_decision

    result = scenario_source_decision(30)
    assert result.master_requests == 0, (
        f"REALTIME master_requests={result.master_requests} > 0（违规）"
    )
    assert result.stale_generation == 0
    assert result.wrong_source_frame == 0
    # source_kinds 应包含 placeholder（无 proxy ready）
    assert "placeholder" in result.source_kinds


def test_seek_stress_scenario_stable_generation():
    """seek_stress 场景 generation 必须稳定，无 stale 堆积。"""
    sys.path.insert(0, str(ROOT))
    from tools.benchmarks.playback_benchmark import scenario_seek_stress

    result = scenario_seek_stress(30)
    assert result.stale_generation == 0
    assert result.wrong_source_frame == 0
    assert result.master_requests == 0


def test_avsync_drift_scenario_returns_valid_stats():
    """AVSync drift 场景必须返回完整 stats。"""
    sys.path.insert(0, str(ROOT))
    from tools.benchmarks.playback_benchmark import scenario_avsync_drift

    stats = scenario_avsync_drift(300)
    assert stats["evaluations"] == 300
    assert "ignores" in stats
    assert "drops" in stats
    assert "holds" in stats
    assert "rebuffers" in stats
    assert stats["max_drift_ms"] >= 0


# =============================================================================
# 3. gate check 正确识别违规
# =============================================================================


def test_gate_check_detects_master_requests_violation():
    """gate check 必须在 realtime_master_requests > 0 时 FAIL。"""
    sys.path.insert(0, str(ROOT))
    from tools.benchmarks.playback_benchmark import (
        BenchmarkReport,
        ScenarioResult,
        check_gates,
    )

    report = BenchmarkReport(timestamp="test")
    report.scenarios.append(ScenarioResult(
        name="test",
        master_requests=3,
    ))
    check_gates(report)
    assert report.overall_pass is False
    assert any("realtime_master_requests" in f for f in report.failures)


def test_gate_check_detects_stale_generation_violation():
    """gate check 必须在 stale_generation > 0 时 FAIL。"""
    sys.path.insert(0, str(ROOT))
    from tools.benchmarks.playback_benchmark import (
        BenchmarkReport,
        ScenarioResult,
        check_gates,
    )

    report = BenchmarkReport(timestamp="test")
    report.scenarios.append(ScenarioResult(
        name="test",
        stale_generation=2,
    ))
    check_gates(report)
    assert report.overall_pass is False
    assert any("stale_generation" in f for f in report.failures)


def test_gate_check_passes_clean_scenario():
    """gate check 在所有指标 = 0 时 PASS。"""
    sys.path.insert(0, str(ROOT))
    from tools.benchmarks.playback_benchmark import (
        BenchmarkReport,
        ScenarioResult,
        check_gates,
    )

    report = BenchmarkReport(timestamp="test")
    report.scenarios.append(ScenarioResult(name="test"))
    check_gates(report)
    assert report.overall_pass is True
    assert report.failures == []
