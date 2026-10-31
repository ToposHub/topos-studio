"""BENCH 批 B（w6-g5）修复测试：--help/--dry-run、字段路径与 CI 计算。

覆盖：
- ``tools/bench_real_4k_h264_hevc.py``（BENCH-0009）：--help 旗标、
  --dry-run（默认/缺媒体告警/非法参数拒绝）、latency_stats 与 d1 同构、
  plan_dict/validate_args 单元；
- ``tools/bench_encode_stages.py``（BENCH-0010）：--dry-run 计划含
  native_profile 状态、native_timing_available 判定（全 0 → False，
  不得写 0 冒充数据）；
- ``tools/bench_spatial_aq_ab.py``（BENCH-0011）：--dry-run、
  bootstrap_ratio_ci（成对百分位法）正确性、verdict_for 预注册判据；
- ``tools/measure_topos_baseline_quality.py``（BENCH-0012）：Metric 的
  MAE 归一化字段——同相对误差在 10/12-bit 域 raw MAE 差 ~4× 而归一化
  相等（2K 4444xq 异常事件的口径复现）；
- ``tests/benchmark/benchmark_timeline_occlusion.py``（BENCH-0015）：
  bootstrap_ratio_ci 的 CI 计算正确性（含退化/None 路径）；
- ``tools/measure_topos_native_domains.py``（BENCH-0016）：
  cli_sha_matches 前缀断言。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = PROJECT_ROOT / "tools"
BENCH_DIR = PROJECT_ROOT / "tests" / "benchmark"
for _p in (str(TOOLS_DIR), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_VENV_PY = PROJECT_ROOT / ".venv" / "bin" / "python"
PYTHON = str(_VENV_PY) if _VENV_PY.is_file() else sys.executable

TOOL_4K = "bench_real_4k_h264_hevc.py"
TOOL_E1 = "bench_encode_stages.py"
TOOL_AQ = "bench_spatial_aq_ab.py"
TOOL_NATIVE = "measure_topos_native_domains.py"
TOOL_QUALITY = "measure_topos_baseline_quality.py"
BENCH_OCCLUSION = "benchmark_timeline_occlusion.py"


def load_module(module_name: str, base: Path):
    """按文件路径加载脚本为模块（脚本自身负责 sys.path）。"""
    spec = importlib.util.spec_from_file_location(module_name, base / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run_tool(script: str, *args: str, base: Path = TOOLS_DIR) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON, str(base / script), *args],
                          capture_output=True, text=True)


# ---------------------------------------------------------------------------
# --help


class TestHelpScreens:
    @pytest.mark.parametrize("script,base", [
        (TOOL_4K, TOOLS_DIR), (TOOL_E1, TOOLS_DIR), (TOOL_AQ, TOOLS_DIR),
        (TOOL_NATIVE, TOOLS_DIR), (BENCH_OCCLUSION, BENCH_DIR),
    ])
    def test_help_exits_zero(self, script, base):
        proc = run_tool(script, "--help", base=base)
        assert proc.returncode == 0, proc.stderr
        assert "usage:" in proc.stdout

    def test_4k_help_flags(self):
        out = run_tool(TOOL_4K, "--help").stdout
        for flag in ("--codecs", "--arms", "--input-h264", "--input-hevc",
                     "--frames", "--repeats", "--warmup", "--random-seeks",
                     "--threads", "--interleave", "--out-root",
                     "--allow-dirty", "--dry-run"):
            assert flag in out, flag

    def test_e1_help_flags(self):
        out = run_tool(TOOL_E1, "--help").stdout
        for flag in ("--no-native-profile", "--expect-commit", "--out-root",
                     "--dry-run", "--allow-dirty"):
            assert flag in out, flag

    def test_aq_help_flags(self):
        out = run_tool(TOOL_AQ, "--help").stdout
        for flag in ("--material", "--tiers", "--frames", "--rounds",
                     "--stride", "--rate-parity", "--psnr-gate",
                     "--fps-floor", "--n-boot", "--seed", "--out-root",
                     "--allow-dirty", "--dry-run"):
            assert flag in out, flag

    def test_native_help_has_record_cli_sha(self):
        out = run_tool(TOOL_NATIVE, "--help").stdout
        assert "--record-cli-sha" in out
        assert "EXPECTED_SHA" in out

    def test_occlusion_help_flags(self):
        out = run_tool(BENCH_OCCLUSION, "--help", base=BENCH_DIR).stdout
        for flag in ("--runs", "--require-cull", "--material", "--output"):
            assert flag in out, flag


# ---------------------------------------------------------------------------
# --dry-run（无媒体可跑通）


class TestDryRun:
    def test_4k_dry_run_default(self):
        proc = run_tool(TOOL_4K, "--dry-run")
        assert proc.returncode == 0, proc.stderr
        assert "[plan]" in proc.stdout
        assert "参数与输出路径自检通过" in proc.stdout
        for arm in ("ffmpeg", "videotoolbox"):
            assert arm in proc.stdout
        for codec in ("h264", "hevc"):
            assert f"input[{codec}]" in proc.stdout
        assert "repeats >= 3" in proc.stdout

    def test_4k_dry_run_missing_media_warns_not_fails(self):
        proc = run_tool(TOOL_4K, "--dry-run",
                        "--input-h264", "/nonexistent/a.mov",
                        "--input-hevc", "/nonexistent/b.mov")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.count("WARNING") == 2

    def test_4k_dry_run_repeats_lt_3_warns(self):
        proc = run_tool(TOOL_4K, "--dry-run", "--repeats", "2")
        assert proc.returncode == 0
        assert "repeats < 3" in proc.stdout

    def test_4k_rejects_bad_params(self):
        assert run_tool(TOOL_4K, "--dry-run", "--frames", "0").returncode != 0
        assert run_tool(TOOL_4K, "--dry-run", "--repeats", "0").returncode != 0
        assert run_tool(TOOL_4K, "--dry-run", "--random-seeks", "-1").returncode != 0
        assert run_tool(TOOL_4K, "--dry-run",
                        "--arms", "nope").returncode != 0
        assert run_tool(TOOL_4K, "--dry-run",
                        "--codecs", "av1").returncode != 0

    def test_aq_dry_run_default(self):
        proc = run_tool(TOOL_AQ, "--dry-run")
        assert proc.returncode == 0, proc.stderr
        assert "[plan]" in proc.stdout
        assert "参数与输出路径自检通过" in proc.stdout
        assert "满足" in proc.stdout  # rounds>=3 纪律行
        # 仓库内默认编码源存在（2K prores Proxy/LT + 母版）
        assert proc.stdout.count("WARNING") == 0

    def test_aq_dry_run_rounds_lt_3_warns(self):
        proc = run_tool(TOOL_AQ, "--dry-run", "--rounds", "2")
        assert proc.returncode == 0
        assert "不满足" in proc.stdout

    def test_aq_rejects_bad_params(self):
        assert run_tool(TOOL_AQ, "--dry-run", "--rounds", "0").returncode != 0
        assert run_tool(TOOL_AQ, "--dry-run", "--frames", "0").returncode != 0
        assert run_tool(TOOL_AQ, "--dry-run", "--stride", "0").returncode != 0
        assert run_tool(TOOL_AQ, "--dry-run",
                        "--rate-parity", "0").returncode != 0
        assert run_tool(TOOL_AQ, "--dry-run",
                        "--tiers", "nope").returncode != 0

    def test_e1_dry_run_reports_native_profile(self):
        proc = run_tool(TOOL_E1, "--dry-run",
                        "--input", "/nonexistent/x.mov")
        assert proc.returncode == 0, proc.stderr
        assert "native_profile" in proc.stdout
        assert "True" in proc.stdout
        assert "WARNING" in proc.stdout  # 输入缺失仅告警

    def test_e1_dry_run_no_native_profile(self):
        proc = run_tool(TOOL_E1, "--dry-run", "--no-native-profile",
                        "--input", "/nonexistent/x.mov")
        assert proc.returncode == 0
        assert "False" in proc.stdout

    def test_e1_rejects_bad_params(self):
        assert run_tool(TOOL_E1, "--dry-run", "--frames", "0").returncode != 0
        assert run_tool(TOOL_E1, "--dry-run", "--qp", "64").returncode != 0


# ---------------------------------------------------------------------------
# BENCH-0009 单元逻辑


class TestReal4kUnits:
    @pytest.fixture(scope="class")
    def mod(self):
        return load_module("bench_real_4k_h264_hevc", TOOLS_DIR)

    def test_latency_stats_d1_shape(self, mod):
        stats = mod.latency_stats([2.0, 4.0, 6.0, 8.0], wall_ms=10.0)
        for key in ("count", "mean_ms", "p50_ms", "p95_ms", "p99_ms",
                    "fps", "fps_by_latency_sum", "wall_ms"):
            assert key in stats, key
        assert stats["p50_ms"] == pytest.approx(5.0)
        # 线性插值：pos = 3 * 0.99 = 2.97 → 6 + 2 * 0.97
        assert stats["p99_ms"] == pytest.approx(7.94)
        assert stats["fps"] == pytest.approx(400.0)

    def test_latency_stats_empty(self, mod):
        stats = mod.latency_stats([], wall_ms=0.0)
        assert stats["count"] == 0 and stats["fps"] == 0.0

    def test_plan_dict_structure(self, mod):
        args = mod.build_arg_parser().parse_args([
            "--dry-run", "--frames", "48", "--repeats", "3",
            "--codecs", "hevc", "--arms", "ffmpeg",
        ])
        plan = mod.plan_dict(args)
        assert plan["codecs"] == ["hevc"]
        assert plan["arms"] == ["ffmpeg"]
        assert plan["repeats_ok"] is True
        assert "hevc" in plan["inputs"] and "hevc" in plan["inputs_exist"]
        assert plan["out_dir_template"].endswith("<utc>")

    def test_validate_rejects(self, mod):
        bad = mod.build_arg_parser().parse_args(["--frames", "0"])
        with pytest.raises(SystemExit):
            mod.validate_args(bad)

    def test_input_override_resolution(self, mod):
        args = mod.build_arg_parser().parse_args([
            "--dry-run", "--input-h264", "/abs/x.mov"])
        inputs = mod.resolve_inputs(args)
        assert str(inputs["h264"]) == "/abs/x.mov"
        assert inputs["hevc"].is_absolute()


# ---------------------------------------------------------------------------
# BENCH-0010 单元逻辑


class TestEncodeStagesUnits:
    @pytest.fixture(scope="class")
    def mod(self):
        return load_module("bench_encode_stages", TOOLS_DIR)

    def test_native_timing_all_zero_unavailable(self, mod):
        stage = SimpleNamespace(**{name: 0 for name in mod.NATIVE_STAGE_NS_FIELDS})
        assert mod.native_timing_available(stage) is False

    def test_native_timing_any_positive_available(self, mod):
        fields = {name: 0 for name in mod.NATIVE_STAGE_NS_FIELDS}
        fields["entropy_ns"] = 12345
        stage = SimpleNamespace(**fields)
        assert mod.native_timing_available(stage) is True

    def test_stage_field_set_matches_binding(self, mod):
        # 与 topos_binding._CEncodeStageStats 的计时字段一致（子集关系）
        assert "fill_ns" in mod.NATIVE_STAGE_NS_FIELDS
        assert "entropy_ns" in mod.NATIVE_STAGE_NS_FIELDS
        assert "check_wall_ns" in mod.NATIVE_STAGE_NS_FIELDS
        assert len(mod.NATIVE_STAGE_NS_FIELDS) == 12

    def test_plan_dict_native_profile_flag(self, mod):
        args = mod.build_arg_parser().parse_args([
            "--dry-run", "--input", "x.mov", "--no-native-profile"])
        plan = mod.plan_dict(args)
        assert plan["native_profile"] is False
        args2 = mod.build_arg_parser().parse_args([
            "--dry-run", "--input", "x.mov"])
        assert mod.plan_dict(args2)["native_profile"] is True


# ---------------------------------------------------------------------------
# BENCH-0011 单元逻辑（bootstrap CI + 判据）


class TestSpatialAqUnits:
    @pytest.fixture(scope="class")
    def mod(self):
        return load_module("bench_spatial_aq_ab", TOOLS_DIR)

    def test_bootstrap_ci_identical_pairs_degenerate(self, mod):
        ci = mod.bootstrap_ratio_ci([(1.0, 1.0), (1.0, 1.0), (1.0, 1.0)],
                                    n_boot=50)
        assert ci is not None
        assert ci["ci_low"] == pytest.approx(1.0)
        assert ci["ci_high"] == pytest.approx(1.0)

    def test_bootstrap_ci_constant_ratio(self, mod):
        # 每对 off=2k, on=k → 比值恒 0.5；任何重采样都是 0.5
        ci = mod.bootstrap_ratio_ci([(4.0, 2.0), (6.0, 3.0), (8.0, 4.0)],
                                    n_boot=50)
        assert ci is not None
        assert ci["ci_low"] == pytest.approx(0.5)
        assert ci["ci_high"] == pytest.approx(0.5)
        assert ci["ratio_median"] == pytest.approx(0.5)

    def test_bootstrap_ci_contains_point_estimate(self, mod):
        paired = [(1.0, 1.2), (1.1, 1.3), (0.9, 1.1), (1.2, 1.4),
                  (1.0, 1.25)]
        ci = mod.bootstrap_ratio_ci(paired, n_boot=2000, seed=7)
        assert ci is not None
        assert ci["ci_low"] <= ci["ratio_median"] + 1e-12
        assert ci["ci_high"] >= ci["ratio_median"] - 1e-12
        assert ci["ci_low"] < ci["ci_high"]
        assert ci["n_boot"] == 2000
        assert ci["confidence"] == pytest.approx(0.95)

    def test_bootstrap_ci_seed_reproducible(self, mod):
        paired = [(1.0, 1.2), (1.1, 1.3), (0.9, 1.1), (1.2, 1.4)]
        a = mod.bootstrap_ratio_ci(paired, n_boot=300, seed=42)
        b = mod.bootstrap_ratio_ci(paired, n_boot=300, seed=42)
        assert a == b

    def test_bootstrap_ci_too_few_or_degenerate(self, mod):
        assert mod.bootstrap_ratio_ci([(1.0, 1.0)]) is None
        assert mod.bootstrap_ratio_ci([]) is None
        assert mod.bootstrap_ratio_ci([(0.0, 1.0), (1.0, 1.0)]) is None

    def test_verdict_rate_mismatch(self, mod):
        v = mod.verdict_for(0.5, 0.10, None, rate_parity=0.02,
                            psnr_gate=0.1, fps_floor=0.9)
        assert v["verdict"] == "rate_mismatch"
        assert v["speed_budget"] == "unknown_rate_mismatch"

    def test_verdict_aq_better_with_speed_budget(self, mod):
        ci = {"ci_low": 0.95, "ci_high": 1.10, "ratio_median": 1.0}
        v = mod.verdict_for(0.2, 0.01, ci, rate_parity=0.02,
                            psnr_gate=0.1, fps_floor=0.9)
        assert v["verdict"] == "aq_better"
        assert v["speed_budget"] == "pass"

    def test_verdict_aq_worse(self, mod):
        v = mod.verdict_for(-0.3, 0.01, None, rate_parity=0.02,
                            psnr_gate=0.1, fps_floor=0.9)
        assert v["verdict"] == "aq_worse"
        assert v["speed_budget"] == "unknown_no_ci"

    def test_verdict_no_effect_and_speed_fail(self, mod):
        ci = {"ci_low": 0.70, "ci_high": 0.85, "ratio_median": 0.8}
        v = mod.verdict_for(0.05, 0.01, ci, rate_parity=0.02,
                            psnr_gate=0.1, fps_floor=0.9)
        assert v["verdict"] == "no_effect"
        assert v["speed_budget"] == "fail"

    def test_plan_dict_tiers_list_or_string(self, mod):
        args = mod.build_arg_parser().parse_args(["--dry-run", "--tiers", "proxy"])
        plan = mod.plan_dict(args)
        assert plan["tiers"] == ["proxy"]
        assert "2k/proxy" in plan["sources"]
        assert plan["rounds_ok"] is True


# ---------------------------------------------------------------------------
# BENCH-0012 单元逻辑（Metric MAE 归一化）


class TestQualityMetricUnits:
    @pytest.fixture(scope="class")
    def mod(self):
        return load_module("measure_topos_baseline_quality", TOOLS_DIR)

    @staticmethod
    def _planes(rng: np.random.Generator, peak: int, shape=(32, 32),
                rel_err: float = 0.004):
        ref = rng.integers(0, peak + 1, size=shape).astype("<u2")
        # 固定相对幅度的对称噪声（截到码值域内）
        amp = max(1.0, peak * rel_err)
        noise = rng.integers(-int(amp), int(amp) + 1, size=shape)
        tp = np.clip(ref.astype(np.int32) + noise, 0, peak).astype("<u2")
        return ref, tp

    def test_mae_domain_mismatch_and_normalization(self, mod):
        """同相对误差下：raw mae 12-bit 域约为 10-bit 域 4×；归一化相等。

        这是 2K 4444xq MAE≈14.3 "异常"的口径复现（quality.md §6.3）。
        """
        rng10 = np.random.default_rng(2026)
        rng12 = np.random.default_rng(2026)
        m10 = mod.Metric("yuv422p10le")
        m12 = mod.Metric("yuv444p12le")
        rel = 0.004
        for _ in range(2):
            for k in range(3):
                r10, t10 = self._planes(rng10, peak=1023, rel_err=rel)
                r12, t12 = self._planes(rng12, peak=4095, rel_err=rel)
                ref10 = [r10, r10, r10]
                tp10 = [t10, t10, t10]
                ref12 = [r12, r12, r12]
                tp12 = [t12, t12, t12]
                ref10[k], tp10[k] = r10, t10
                ref12[k], tp12[k] = r12, t12
            m10.add(tuple(ref10), tuple(tp10))
            m12.add(tuple(ref12), tuple(tp12))
        r10, r12 = m10.result(), m12.result()
        # 新字段存在
        assert r10["bit_depth"] == 10 and r12["bit_depth"] == 12
        for name in ("mae_y", "mae_y_norm", "mae_y_10bit"):
            assert name in r10, name
        # 域混排：raw mae 差接近 4×（跨域直比无效的根源）
        ratio_raw = r12["mae_y"] / r10["mae_y"]
        assert 3.5 < ratio_raw < 4.6
        # 归一化后跨域可比
        assert r12["mae_y_norm"] == pytest.approx(r10["mae_y_norm"],
                                                 rel=0.25)
        assert r12["mae_y_10bit"] == pytest.approx(r10["mae_y"], rel=0.25)

    def test_mae_cross_domain_comparison_restores_monotonicity(self, mod):
        """复现 4444(10-bit) vs 4444xq(12-bit) 的档位排序：raw 直比给出
        错误结论（xq 更差），归一化/10-bit 等效恢复正确排序。"""
        rng = np.random.default_rng(99)
        m10 = mod.Metric("yuv444p10le")
        m12 = mod.Metric("yuv444p12le")
        base = np.full((16, 16), 500, dtype="<u2")
        ref = tuple(base.astype("<u2") for _ in range(3))
        tp10 = tuple(np.clip(base.astype(np.int32)
                             + rng.integers(-4, 5, size=base.shape),
                            0, 1023).astype("<u2") for _ in range(3))
        tp12 = tuple(np.clip(base.astype(np.int32)
                             + rng.integers(-12, 13, size=base.shape),
                            0, 4095).astype("<u2") for _ in range(3))
        m10.add(ref, ref)  # 先加一对相同帧避免退化
        m10.add(ref, tp10)
        m12.add(ref, ref)
        m12.add(ref, tp12)
        r10, r12 = m10.result(), m12.result()
        # raw 直比：12-bit 域数值 ~3× 大于 10-bit（历史"异常"观感）
        assert r12["mae_y"] > r10["mae_y"] * 2
        # 10-bit 等效：12-bit ±12 code ≈ 10-bit ±3 code，与 ±4 同级
        assert r12["mae_y_10bit"] == pytest.approx(r10["mae_y"], rel=0.5)
        assert r12["mae_y_norm"] == pytest.approx(r10["mae_y_norm"], rel=0.5)


# ---------------------------------------------------------------------------
# BENCH-0015 单元逻辑（timeline_occlusion bootstrap CI）


class TestOcclusionBootstrap:
    @pytest.fixture(scope="class")
    def mod(self):
        return load_module("benchmark_timeline_occlusion", BENCH_DIR)

    def test_ci_constant_speedup_degenerate(self, mod):
        # 每轮 on=1.0 / off=3.67 → speedup 恒 3.67（B1 观察值口径）
        ci = mod.bootstrap_ratio_ci([(1.0, 3.67), (1.0, 3.67), (1.0, 3.67)],
                                    n_boot=50)
        assert ci is not None
        assert ci["ci_low"] == pytest.approx(3.67)
        assert ci["ci_high"] == pytest.approx(3.67)

    def test_ci_spread_with_noise(self, mod):
        paired = [(1.0, 2.0), (1.2, 2.6), (0.9, 1.7), (1.1, 2.1),
                  (1.3, 2.9)]
        ci = mod.bootstrap_ratio_ci(paired, n_boot=2000, seed=11)
        assert ci is not None
        assert ci["ci_low"] < ci["ci_high"]
        # 点估计落在区间内（中位比 ≈ 2.0）
        assert ci["ci_low"] <= 2.1 and ci["ci_high"] >= 1.9

    def test_ci_none_paths(self, mod):
        assert mod.bootstrap_ratio_ci([(1.0, 2.0)]) is None  # <2 对
        assert mod.bootstrap_ratio_ci([]) is None
        assert mod.bootstrap_ratio_ci([(0.0, 2.0), (1.0, 2.0)]) is None

    def test_ci_seed_reproducible(self, mod):
        paired = [(1.0, 2.0), (1.2, 2.6), (0.9, 1.7), (1.1, 2.1)]
        a = mod.bootstrap_ratio_ci(paired, n_boot=200, seed=5)
        b = mod.bootstrap_ratio_ci(paired, n_boot=200, seed=5)
        assert a == b
        assert a["method"].startswith("paired percentile bootstrap")


# ---------------------------------------------------------------------------
# BENCH-0016 单元逻辑（CLI SHA 断言）


class TestCliShaMatch:
    @pytest.fixture(scope="class")
    def mod(self):
        return load_module("measure_topos_native_domains", TOOLS_DIR)

    def test_full_sha_match(self, mod):
        sha = "06508c5c" + "a" * 56
        assert mod.cli_sha_matches(sha, sha) is True
        assert mod.cli_sha_matches(sha, sha.upper()) is True

    def test_prefix_match_requires_min_8(self, mod):
        sha = "333b7df3" + "b" * 56
        assert mod.cli_sha_matches(sha, "333b7df3") is True
        assert mod.cli_sha_matches(sha, "333B7DF3") is True
        # <8 位前缀不接受（防过宽匹配）
        assert mod.cli_sha_matches(sha, "333b7df") is False

    def test_mismatch(self, mod):
        sha = "4b8e5366" + "c" * 56
        assert mod.cli_sha_matches(sha, "06508c5c") is False
        assert mod.cli_sha_matches(sha, "06508c5c" + "a" * 56) is False

    def test_empty_expected_records_only(self, mod):
        assert mod.cli_sha_matches("whatever", "") is True
        assert mod.cli_sha_matches("whatever", None) is True
