"""P0-12/P0-09: 性能预算注册表与回归门禁单元测试。"""

import json

import pytest

from src.shared.video.performance_budgets import (
    Budget,
    compare_reports,
    evaluate_report_against_budgets,
    gate_summary,
    iter_budgets,
    load_budgets,
)


@pytest.fixture(scope="module")
def registry():
    return load_budgets()


def _runner_report(media):
    """构造 runner 报告形状（与 tests/benchmark/benchmark_runner 输出一致）。"""
    return {
        "$schema_version": 1,
        "machine": {"platform": "macOS-15.3-x86_64"},
        "media": media,
    }


def _media_entry(name, steady_p95, seek_p95=None, cold_p95=None):
    phases = {"steady_sequential": {"p95_ms": steady_p95, "p50_ms": steady_p95 / 2}}
    if seek_p95 is not None:
        phases["seek_random"] = {"p95_ms": seek_p95}
    if cold_p95 is not None:
        phases["cold_first_frame"] = {"p95_ms": cold_p95}
    return {"file": f"tests/test_videos/{name}", "phases": phases}


class TestRegistry:
    def test_loads_real_registry(self, registry):
        assert registry["$schema_version"] == 1
        budgets = iter_budgets(registry)
        assert len(budgets) >= 8
        ids = [b.budget_id for b in budgets]
        assert len(ids) == len(set(ids))

    def test_hard_gate_is_looser_than_soft(self, registry):
        for b in iter_budgets(registry):
            assert b.soft_max_ms <= b.hard_max_ms, b.budget_id

    def test_known_breaches_carry_remediation_note(self, registry):
        for b in iter_budgets(registry):
            if b.state == "known_breach":
                assert b.note.strip(), b.budget_id
                assert "P" in b.note, f"{b.budget_id} 缺责任工作项"

    def test_bad_state_raises(self):
        with pytest.raises(ValueError):
            iter_budgets({"budgets": [{
                "budget_id": "X", "scenario": "B-01", "metric": "a.b",
                "media_glob": "*", "hard_max_ms": 1.0, "soft_max_ms": 0.5,
                "state": "paused",
            }]})


class TestEvaluate:
    def test_pass_and_soft_and_hard(self, registry):
        report = _runner_report([
            _media_entry("2K_h264.mp4", steady_p95=10.0, seek_p95=80.0,
                         cold_p95=200.0),
        ])
        verdicts = evaluate_report_against_budgets(report, registry)
        by_id = {v["budget_id"]: v["verdict"] for v in verdicts}
        # steady 10 < soft 16.7 → pass；seek 80 < soft 120 → pass
        assert by_id["BUD-B01-STEADY-P95"] == "pass"
        assert by_id["BUD-B01-SEEK-P95"] == "pass"
        # steady 20：16.7 < 20 <= 32 → soft_breach
        report_soft = _runner_report([
            _media_entry("2K_h264.mp4", steady_p95=20.0, seek_p95=80.0),
        ])
        v2 = {x["budget_id"]: x["verdict"]
              for x in evaluate_report_against_budgets(report_soft, registry)}
        assert v2["BUD-B01-STEADY-P95"] == "soft_breach"
        # steady 40 > hard 32 → hard_breach
        report_hard = _runner_report([
            _media_entry("2K_h264.mp4", steady_p95=40.0, seek_p95=80.0),
        ])
        v3 = {x["budget_id"]: x["verdict"]
              for x in evaluate_report_against_budgets(report_hard, registry)}
        assert v3["BUD-B01-STEADY-P95"] == "hard_breach"

    def test_known_breach_downgrades_hard(self, registry):
        # 4K H265 seek 519ms：超 hard，但已登记免责（P2-05）。
        report = _runner_report([
            _media_entry("4K_H265.mov", steady_p95=20.0, seek_p95=519.0),
        ])
        verdicts = evaluate_report_against_budgets(report, registry)
        by_id = {v["budget_id"]: v for v in verdicts}
        assert by_id["BUD-B02-SEEK-P95"]["verdict"] == "known_breach"
        assert "P2-05" in by_id["BUD-B02-SEEK-P95"]["note"]

    def test_missing_media_is_gate_failure(self, registry):
        report = _runner_report([])  # 什么媒体都没跑
        verdicts = evaluate_report_against_budgets(report, registry)
        assert all(v["verdict"] == "missing" for v in verdicts)

    def test_glob_matches_multiple_prores(self, registry):
        report = _runner_report([
            _media_entry("4K_prores422HQ.mov", steady_p95=38.2, seek_p95=37.0),
            _media_entry("4K_prores422LT.mov", steady_p95=28.8, seek_p95=31.0),
        ])
        verdicts = evaluate_report_against_budgets(report, registry)
        steady = [v for v in verdicts
                  if v["budget_id"] == "BUD-B03-STEADY-P95"]
        # 取第一个匹配（HQ 38.2 > hard 32，known_breach 状态）
        assert steady[0]["verdict"] == "known_breach"


class TestCompareReports:
    def test_regression_detected(self):
        old = _runner_report([_media_entry("2K_h264.mp4", steady_p95=10.0,
                                           seek_p95=60.0)])
        new = _runner_report([_media_entry("2K_h264.mp4", steady_p95=10.8,
                                           seek_p95=60.0)])
        out = compare_reports(new, old, threshold_pct=5.0)
        regs = [r for r in out if r["verdict"] == "regression"]
        # steady p95=10.8（+8%）；_media_entry 的 p50=p95/2 也同比 +8%。
        assert {r["metric"] for r in regs} == {"p50_ms", "p95_ms"}
        p95 = next(r for r in regs if r["metric"] == "p95_ms")
        assert p95["delta_pct"] == pytest.approx(8.0, abs=0.1)

    def test_improvement_not_flagged(self):
        old = _runner_report([_media_entry("2K_h264.mp4", steady_p95=10.0)])
        new = _runner_report([_media_entry("2K_h264.mp4", steady_p95=5.0)])
        assert compare_reports(new, old) == []

    def test_cross_machine_not_judged(self):
        old = _runner_report([_media_entry("2K_h264.mp4", steady_p95=10.0)])
        new = _runner_report([_media_entry("2K_h264.mp4", steady_p95=20.0)])
        new["machine"]["platform"] = "Windows-11"
        out = compare_reports(new, old)
        assert all(r["verdict"] == "cross_machine" for r in out)

    def test_zero_reference_skipped(self):
        old = _runner_report([_media_entry("2K_h264.mp4", steady_p95=0.0)])
        new = _runner_report([_media_entry("2K_h264.mp4", steady_p95=10.0)])
        assert compare_reports(new, old) == []


class TestGateSummary:
    def test_exit_codes(self):
        assert gate_summary([], [])["exit_code"] == 0
        assert gate_summary([{"verdict": "soft_breach"}], [])["exit_code"] == 1
        assert gate_summary([], [{"verdict": "regression"}])["exit_code"] == 1
        assert gate_summary([{"verdict": "hard_breach"}], [])["exit_code"] == 2
        assert gate_summary([{"verdict": "missing"}], [])["exit_code"] == 2
        s = gate_summary([{"verdict": "known_breach"}], [])
        assert s["exit_code"] == 0  # 免责不阻止


class TestGateCli:
    def test_real_baseline_passes_gate(self, tmp_path):
        """完整 B-01..B-04 基线报告（含 known_breach 缺口）必须通过门禁。"""
        repo = load_budgets.__globals__["REPO_ROOT"]
        baseline = None
        for p in sorted((repo / "bench_out" / "p0_runner").glob("*.json")):
            try:
                files = {m.get("file", "") for m in
                         json.loads(p.read_text()).get("media", [])}
            except Exception:  # noqa: BLE001
                continue
            if any(f.endswith("4K_H265.mov") for f in files):
                baseline = p  # 覆盖 B-01..B-04 的完整基线
        if baseline is None:
            pytest.skip("no full baseline report available")
        import subprocess
        import sys

        gate = repo / "tools" / "perf_regression_gate.py"
        proc = subprocess.run(
            [sys.executable, str(gate), "--report", str(baseline)],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # H265 seek（P2-05）+ ProRes steady（P2-02 接管）两个已登记缺口
        assert "known=2" in proc.stdout
