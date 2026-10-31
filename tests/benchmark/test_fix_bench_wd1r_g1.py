"""批 D 重派 G1（wd1r-g1）修复测试：BENCH-0003/0007/0008/0013/0014/0017。

覆盖（对齐 test_fix_bench_w6_aud6.py 风格——脚本按文件路径加载，
子进程跑 --help/--dry-run，纯函数直接单测）：

- ``tools/bench_wrapper_binding_overhead.py``（BENCH-0003）：--help/--dry-run、
  verdict_overhead 预注册判据全路径（含门禁）；
- ``tools/bench_default_config_playback.py``（BENCH-0007/0008）：--dry-run、
  paced_playback 节拍消费（dropped/late 计数，时钟注入）、parse_depths；
- ``tools/bench_q01_rd_curve.py``（BENCH-0013）：RD 插值、码率/画质匹配
  两表、Q04 判定（含 ≥5 点硬门槛）、--dry-run、12-bit 拒绝；
- ``tools/bench_thread_pivot_gate.py``（BENCH-0014）：pivot_verdict 五种
  判定、parse_threads、--dry-run；
- ``tools/check_regression_anchors.py``（BENCH-0017）：source 字段解析
  （分号/中文括注/花括号/glob）、P0-04 树分类（含伪树拒绝）、完整性
  缺失 → exit 1、--dry-run 不写盘。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = PROJECT_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_VENV_PY = PROJECT_ROOT / ".venv" / "bin" / "python"
PYTHON = str(_VENV_PY) if _VENV_PY.is_file() else sys.executable

TOOL_OVERHEAD = "bench_wrapper_binding_overhead.py"
TOOL_PLAYBACK = "bench_default_config_playback.py"
TOOL_RD = "bench_q01_rd_curve.py"
TOOL_PIVOT = "bench_thread_pivot_gate.py"
TOOL_ANCHORS = "check_regression_anchors.py"
ALL_TOOLS = [TOOL_OVERHEAD, TOOL_PLAYBACK, TOOL_RD, TOOL_PIVOT,
             TOOL_ANCHORS]


def load_module(module_name: str, base: Path):
    """按文件路径加载脚本为模块（脚本自身负责 sys.path）。"""
    spec = importlib.util.spec_from_file_location(
        module_name, base / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run_tool(script: str, *args: str, base: Path = TOOLS_DIR
             ) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON, str(base / script), *args],
                          capture_output=True, text=True)


# ---------------------------------------------------------------------------
# --help / --dry-run


@pytest.mark.parametrize("script", ALL_TOOLS)
def test_help_screens(script):
    result = run_tool(script, "--help")
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


def test_overhead_dry_run_plan():
    result = run_tool(TOOL_OVERHEAD, "--dry-run")
    assert result.returncode == 0
    assert "topos_wrapper vs topos_native" in result.stdout
    assert ">=3 纪律" in result.stdout  # pairs 纪律行


def test_overhead_dry_run_rejects_bad_args():
    assert run_tool(TOOL_OVERHEAD, "--pairs", "0", "--dry-run").returncode != 0
    assert run_tool(TOOL_OVERHEAD, "--warmup", "10", "--frames", "10",
                    "--dry-run").returncode != 0
    assert run_tool(TOOL_OVERHEAD, "--max-overhead", "1.0",
                    "--dry-run").returncode != 0


def test_playback_dry_run_plan():
    result = run_tool(TOOL_PLAYBACK, "--dry-run")
    assert result.returncode == 0
    assert "readahead_auto vs readahead_8" in result.stdout
    # §4.2 遥测声明：测得到的与测不到的都必须列名（诚实性）
    assert "queue_depth" in result.stdout
    assert "texture_write" in result.stdout


def test_rd_dry_run_and_gates():
    result = run_tool(TOOL_RD, "--source-raw", "x.raw",
                      "--source-origin", "decoded-lossy-source",
                      "--width", "1920", "--height", "1080",
                      "--frames", "30", "--dry-run")
    assert result.returncode == 0
    assert "20, 32, 44, 56, 63" in result.stdout.replace("[", "").replace(
        "]", "")
    # 12-bit 444 档显式拒绝（防 prores_ks 静默位深转换伪比较）
    assert run_tool(TOOL_RD, "--source-raw", "x.raw", "--source-origin", "s",
                    "--width", "1920", "--height", "1080", "--frames", "30",
                    "--tier", "4444xq", "--dry-run").returncode != 0
    # <5 质量点拒绝（决策文档 Q04 硬要求）
    assert run_tool(TOOL_RD, "--source-raw", "x.raw", "--source-origin", "s",
                    "--width", "1920", "--height", "1080", "--frames", "30",
                    "--topos-qps", "20,30,40", "--dry-run").returncode != 0


def test_pivot_dry_run_plan():
    result = run_tool(TOOL_PIVOT, "--dry-run")
    assert result.returncode == 0
    assert "8, 12, 16" in result.stdout
    assert "pivot_justified" in result.stdout
    # threads 必须升序且 ≥2 档
    assert run_tool(TOOL_PIVOT, "--threads", "16,8",
                    "--dry-run").returncode != 0
    assert run_tool(TOOL_PIVOT, "--threads", "8",
                    "--dry-run").returncode != 0


def test_anchors_dry_run_writes_nothing(tmp_path):
    registry_root = PROJECT_ROOT / "bench_out" / "anchor_registry"

    def _snapshot() -> set:
        return {str(p.relative_to(registry_root))
                for p in registry_root.rglob("*")} \
            if registry_root.is_dir() else set()

    before = _snapshot()
    result = run_tool(TOOL_ANCHORS, "--dry-run")
    assert result.returncode == 0
    assert "P04" in result.stdout
    assert _snapshot() == before  # dry-run 不得新增/改动任何登记产物


# ---------------------------------------------------------------------------
# BENCH-0003：verdict_overhead 预注册判据


def _ci(low: float, high: float) -> dict:
    return {"ratio_median": (low + high) / 2, "ci_low": low, "ci_high": high}


class TestOverheadVerdict:
    mod = load_module("bench_wrapper_binding_overhead", TOOLS_DIR)

    def test_insufficient_pairs(self):
        verdict = self.mod.verdict_overhead(_ci(2.0, 2.5), pairs=2)
        assert verdict["verdict"] == "insufficient_pairs"

    def test_degenerate(self):
        verdict = self.mod.verdict_overhead(None, pairs=12)
        assert verdict["verdict"] == "degenerate"

    def test_overhead_open_2x(self):
        # 现状形态：CI 下界 > 2.0（real_2k 证据 2.3–2.5×）
        verdict = self.mod.verdict_overhead(_ci(2.28, 2.55), pairs=12)
        assert verdict["verdict"] == "overhead_open_2x"
        assert not verdict["gate_failed"]

    def test_binding_at_parity(self):
        # m11 式绑定修复兑现后的验收线：CI 上界 < 1.2
        verdict = self.mod.verdict_overhead(_ci(1.02, 1.15), pairs=12)
        assert verdict["verdict"] == "binding_at_parity"

    def test_overhead_partial(self):
        verdict = self.mod.verdict_overhead(_ci(1.3, 1.9), pairs=12)
        assert verdict["verdict"] == "overhead_partial"

    def test_gate_failure(self):
        verdict = self.mod.verdict_overhead(_ci(2.6, 2.9), pairs=12,
                                            max_overhead=2.5)
        assert verdict["verdict"] == "overhead_open_2x"
        assert verdict["gate_failed"] and verdict["gate"] == 2.5

    def test_gate_pass_when_ci_low_within(self):
        verdict = self.mod.verdict_overhead(_ci(2.4, 2.6), pairs=12,
                                            max_overhead=2.5)
        assert not verdict["gate_failed"]


# ---------------------------------------------------------------------------
# BENCH-0007/0008：paced_playback 与深度解析


class TestPacedPlayback:
    mod = load_module("bench_default_config_playback", TOOLS_DIR)

    @staticmethod
    def _make_clock(ticks):
        state = {"i": 0}

        def clock() -> float:
            now = ticks[state["i"]] if state["i"] < len(ticks) \
                else ticks[-1] + 1.0
            state["i"] += 1
            return now

        return clock

    def test_no_drops_no_lates_when_fast(self):
        # 每 40ms 一槽、解码恒 5ms、时钟恒准点 → 0 dropped 0 late
        clock = self._make_clock([0.040, 0.045, 0.080, 0.085,
                                  0.120, 0.125, 99.0])
        result = self.mod.paced_playback(
            lambda _i: 0.005, total=3, period_s=0.040,
            clock=clock, sleeper=lambda _s: None)
        assert result["dropped_frames"] == 0
        assert result["late_frames"] == 0
        assert result["count"] == 3

    def test_late_counted_when_decode_exceeds_period(self):
        clock = self._make_clock([0.040, 0.081, 0.080, 0.121, 0.120, 99.0])
        result = self.mod.paced_playback(
            lambda _i: 0.050, total=2, period_s=0.040,
            clock=clock, sleeper=lambda _s: None)
        assert result["count"] == 2
        assert result["late_frames"] == 2
        assert result["dropped_frames"] == 0

    def test_dropped_when_behind_by_full_period(self):
        # start=0、第 0 帧槽 40ms，取帧时已 90ms（> 40+40）→ 第 0 帧被跳过
        clock = self._make_clock([0.000, 0.090, 0.091, 99.0])
        pulled: list[int] = []
        result = self.mod.paced_playback(
            lambda i: pulled.append(i) or 0.005, total=2, period_s=0.040,
            clock=clock, sleeper=lambda _s: None)
        assert result["dropped_frames"] == 1
        assert result["count"] == 1
        assert pulled == [1]  # 落后的第 0 帧未被拉取

    def test_parse_depths_and_labels(self):
        assert self.mod.parse_depths("auto,8") == [None, 8]
        assert self.mod.parse_depths("4") == [4]
        assert self.mod.config_label(None) == "readahead_auto"
        assert self.mod.config_label(8) == "readahead_8"
        with pytest.raises(SystemExit):
            self.mod.parse_depths("")
        with pytest.raises(SystemExit):
            self.mod.parse_depths("auto,-1")


# ---------------------------------------------------------------------------
# BENCH-0013：RD 插值 / 两表 / Q04 判定


class TestRdCurve:
    mod = load_module("bench_q01_rd_curve", TOOLS_DIR)

    def test_interp_bits_at_psnr(self):
        points = [{"psnr_y": 40.0, "bits": 100},
                  {"psnr_y": 50.0, "bits": 200},
                  {"psnr_y": 60.0, "bits": 400}]
        assert self.mod.interp_bits_at_psnr(points, 45.0) == 150.0
        assert self.mod.interp_bits_at_psnr(points, 50.0) == 200.0
        assert self.mod.interp_bits_at_psnr(points, 39.0) is None
        assert self.mod.interp_bits_at_psnr(points, 61.0) is None

    def test_rate_matched_table(self):
        topos = [{"qp": 30, "bits": 100, "psnr_y": 50.0},
                 {"qp": 50, "bits": 202, "psnr_y": 40.0}]
        prores = [{"bits_per_mb": 128, "bits": 101, "psnr_y": 42.0},
                  {"bits_per_mb": 256, "bits": 250, "psnr_y": 48.0}]
        rows = self.mod.rate_matched_table(topos, prores)
        by_qp = {r["topos_qp"]: r for r in rows}
        assert by_qp[30]["match"] == "matched"      # 100/101 在 ±2%
        assert by_qp[30]["delta_psnr_y_db"] == pytest.approx(8.0)
        assert by_qp[50]["match"] == "nearest_below"  # 202/250 → topos 更低

    def test_quality_matched_table(self):
        topos = [{"qp": 30, "bits": 90, "psnr_y": 50.0},
                 {"qp": 50, "bits": 45, "psnr_y": 40.0}]
        prores = [{"bits_per_mb": 128, "bits": 100, "psnr_y": 45.0},
                  {"bits_per_mb": 64, "bits": 60, "psnr_y": 35.0}]
        rows = self.mod.quality_matched_table(topos, prores)
        assert rows[0]["topos_bits_at_same_psnr"] == pytest.approx(67.5)
        assert rows[0]["bits_ratio_topos_over_prores"] == pytest.approx(
            0.675)
        assert rows[1]["in_range"] is False  # 35 dB 超出 topos 曲线范围

    def test_q04_verdict_paths(self):
        topos5 = [{"qp": q, "bits": b, "psnr_y": p} for q, b, p in
                  ((20, 400, 60.0), (32, 200, 50.0), (44, 100, 40.0),
                   (56, 50, 30.0), (63, 25, 22.0))]
        prores5 = [{"bits_per_mb": m, "bits": b * 1.25, "psnr_y": p}
                   for m, b, p in ((64, 400, 60.0), (128, 200, 50.0),
                                   (256, 100, 40.0), (512, 50, 30.0),
                                   (1024, 25, 22.0))]
        quality = self.mod.quality_matched_table(topos5, prores5)
        rate = self.mod.rate_matched_table(topos5, prores5)
        # 每点同 PSNR 下 topos 恒为 prores 的 0.8 → cheaper
        verdict = self.mod.q04_verdict(rate, quality, 5, 5)
        assert verdict["verdict"] == "topos_same_quality_cheaper"
        # 不足 5 点 → 硬拒绝
        small = self.mod.q04_verdict(rate[:2], quality[:2], 2, 5)
        assert small["verdict"] == "insufficient_points"
        # 更贵方向
        costlier = [{"bits_per_mb": m, "bits": b * 0.7, "psnr_y": p}
                    for m, b, p in ((64, 400, 60.0), (128, 200, 50.0),
                                    (256, 100, 40.0), (512, 50, 30.0),
                                    (1024, 25, 22.0))]
        quality2 = self.mod.quality_matched_table(topos5, costlier)
        assert self.mod.q04_verdict(
            rate, quality2, 5, 5)["verdict"] == \
            "topos_costs_more_at_same_quality"
        # PSNR 重叠不足
        narrow = [{"qp": 1, "bits": 10, "psnr_y": 80.0}] * 5
        quality3 = self.mod.quality_matched_table(narrow, prores5)
        assert self.mod.q04_verdict(
            rate, quality3, 5, 5)["verdict"] == "insufficient_quality_overlap"

    def test_parse_int_list(self):
        assert self.mod.parse_int_list("1, 2,3", "x", 0, 63) == [1, 2, 3]
        with pytest.raises(SystemExit):
            self.mod.parse_int_list("1,1", "x", 0, 63)
        with pytest.raises(SystemExit):
            self.mod.parse_int_list("64", "x", 0, 63)
        with pytest.raises(SystemExit):
            self.mod.parse_int_list("", "x", 0, 63)


# ---------------------------------------------------------------------------
# BENCH-0014：pivot_verdict 判定门


def _agg(wall_mean: float, p95: float, share: float | None) -> dict:
    return {"wall_mean_ms_median": wall_mean, "wall_p95_ms_median": p95,
            "entropy_fill_share_median": share}


class TestPivotVerdict:
    mod = load_module("bench_thread_pivot_gate", TOOLS_DIR)

    def test_pivot_justified_current_shape(self):
        # 试点现状形态：12→16 ≈ +1.5%、16T p95 41.6 > 40、熵段份额 0.5
        agg = {8: _agg(39.43, 48.23, 0.55), 12: _agg(34.37, 43.49, 0.52),
               16: _agg(33.86, 41.63, 0.50)}
        verdict = self.mod.pivot_verdict(agg, budget_ms=40.0,
                                         marginal_threshold_pct=5.0,
                                         entropy_floor=0.30, round_count=3)
        assert verdict["verdict"] == "pivot_justified"
        assert verdict["thread_axis"] == "saturated"
        assert verdict["marginal_gain_pct"] == pytest.approx(1.48, abs=0.1)

    def test_threads_still_profitable(self):
        agg = {8: _agg(40.0, 50.0, 0.5), 16: _agg(30.0, 41.0, 0.5)}
        verdict = self.mod.pivot_verdict(agg, 40.0, 5.0, 0.30, 3)
        assert verdict["verdict"] == "threads_still_profitable"

    def test_budget_met_no_pivot(self):
        agg = {8: _agg(40.0, 50.0, 0.5), 16: _agg(39.0, 35.0, 0.5)}
        verdict = self.mod.pivot_verdict(agg, 40.0, 5.0, 0.30, 3)
        assert verdict["verdict"] == "budget_met_no_pivot"

    def test_pivot_target_unclear_low_share(self):
        agg = {8: _agg(40.0, 50.0, 0.2), 16: _agg(39.5, 45.0, 0.1)}
        verdict = self.mod.pivot_verdict(agg, 40.0, 5.0, 0.30, 3)
        assert verdict["verdict"] == "pivot_target_unclear"

    def test_pivot_target_unclear_no_timing(self):
        agg = {8: _agg(40.0, 50.0, None), 16: _agg(39.5, 45.0, None)}
        verdict = self.mod.pivot_verdict(agg, 40.0, 5.0, 0.30, 3)
        assert verdict["verdict"] == "pivot_target_unclear"

    def test_insufficient_rounds(self):
        verdict = self.mod.pivot_verdict(
            {8: _agg(40, 50, 0.5), 16: _agg(39.5, 45, 0.5)},
            40.0, 5.0, 0.30, round_count=1)
        assert verdict["verdict"] == "insufficient_rounds"

    def test_parse_threads(self):
        assert self.mod.parse_threads("8,12,16") == [8, 12, 16]
        with pytest.raises(SystemExit):
            self.mod.parse_threads("8")
        with pytest.raises(SystemExit):
            self.mod.parse_threads("16,8")
        with pytest.raises(SystemExit):
            self.mod.parse_threads("8,8")


# ---------------------------------------------------------------------------
# BENCH-0017：锚点登记


class TestAnchorRegistry:
    mod = load_module("check_regression_anchors", TOOLS_DIR)

    def test_extract_source_paths(self):
        field = ("bench_out/a/result.json（decode 段）; bench_out/b.txt; "
                 "bench_out/{proxy,lt}-match/paired_*.json")
        paths = self.mod.extract_source_paths(field)
        assert paths == ["bench_out/a/result.json", "bench_out/b.txt",
                         "bench_out/proxy-match/paired_*.json",
                         "bench_out/lt-match/paired_*.json"]

    def test_extract_strips_all_parens(self):
        paths = self.mod.extract_source_paths(
            "bench_out/x.json（引用以 v2 为准：脚本 SHA a97ab678…）")
        assert paths == ["bench_out/x.json"]

    def test_classify_p04_tree(self, tmp_path):
        tree = tmp_path / "bench_out" / "fam" / "aaaa1111" / "bbbb2222" / (
            "cccc3333") / "20260928T000000Z"
        tree.mkdir(parents=True)
        (tree / "result.json").write_text(json.dumps(
            {"manifest": {"schema": "bench_manifest.v1", "git_head":
                          "aaaa1111", "git_dirty": True,
                          "dylib_sha256": "dddd4444dddd"}}), encoding="utf-8")
        info = self.mod.classify_source(tree / "result.json", tmp_path)
        assert info["classification"] == "p04_manifest_tree"
        assert info["manifest"]["git_head"] == "aaaa1111"
        assert info["manifest"]["dylib_sha8"] == "dddd4444"
        assert info["manifest"]["git_dirty"] is True

    def test_classify_rejects_shape_without_manifest(self, tmp_path):
        # 目录形状对但文件无 bench_manifest.v1 → 不得判 P04（防伪树）
        tree = tmp_path / "bench_out" / "fam" / "aaaa1111" / "bbbb2222" / (
            "cccc3333") / "20260928T000000Z"
        tree.mkdir(parents=True)
        (tree / "result.json").write_text("{\"manifest\": null}",
                                          encoding="utf-8")
        info = self.mod.classify_source(tree / "result.json", tmp_path)
        assert info["classification"] == "bench_out_off_tree"

    def test_classify_off_tree_kinds(self, tmp_path):
        benchmarks = tmp_path / "benchmarks" / "result.json"
        info = self.mod.classify_source(benchmarks, tmp_path)
        assert info["classification"] == "repo_benchmarks_off_tree"
        flat = tmp_path / "bench_out" / "readahead" / "x.txt"
        info = self.mod.classify_source(flat, tmp_path)
        assert info["classification"] == "bench_out_off_tree"
        info = self.mod.classify_source(tmp_path / "docs" / "x.md", tmp_path)
        assert info["classification"] == "other_off_tree"

    def test_audit_baseline_missing_source(self, tmp_path):
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps([
            {"name": "ANC-01 x", "source": "bench_out/gone/result.json",
             "tolerance": "t"}]), encoding="utf-8")
        audit = self.mod.audit_baseline(baseline, repo_root=tmp_path)
        assert audit["anchors"][0]["status"] == "missing"
        assert audit["summary"]["anchors_missing"] == 1

    def test_audit_baseline_roundtrip(self, tmp_path):
        tree = tmp_path / "bench_out" / "d1" / "aaaa1111" / "bbbb2222" / (
            "cccc3333") / "20260928T000000Z"
        tree.mkdir(parents=True)
        (tree / "result.json").write_text(json.dumps(
            {"manifest": {"schema": "bench_manifest.v1",
                          "git_head": "aaaa1111", "git_dirty": False,
                          "dylib_sha256": "x" * 64}}), encoding="utf-8")
        notes = tmp_path / "bench_out" / "notes.txt"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("x", encoding="utf-8")
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps([
            {"name": "ANC-01 ok", "source":
             f"{tree / 'result.json'}; bench_out/notes.txt",
             "tolerance": "t"}]), encoding="utf-8")
        audit = self.mod.audit_baseline(baseline, repo_root=tmp_path)
        anchor = audit["anchors"][0]
        assert anchor["status"] == "ok"
        kinds = {e["classification"] for e in anchor["sources"]}
        assert kinds == {"p04_manifest_tree", "bench_out_off_tree"}
        assert audit["summary"]["sources_p04_manifest_tree"] == 1
        assert audit["baseline_sha256"]
