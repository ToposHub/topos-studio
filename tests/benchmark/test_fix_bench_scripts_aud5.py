"""BENCH 批 A（w5-g5）修复测试：bench 脚本 --help/--dry-run 与预算门禁判定。

覆盖：
- ``tools/bench_real_2k_decode_4arm.py`` / ``tools/bench_rd_scan_repro.py`` /
  ``tools/check_decode_budget.py`` / ``tools/bench_topos_playback_path.py``
  的 ``--help``（关键旗标齐全）；
- 两个新基准脚本的 ``--dry-run``（无媒体环境自检参数/输出路径，退出码 0；
  非法参数被拒绝）；
- ``check_decode_budget`` 判定逻辑（tmp_path 造 d1 形状的 result.json：
  超支 exit 1 / 在预算内 exit 0 / --budget-ms 覆盖 / --min-height 过滤 /
  缺 p99 字段跳过 / random_seek 不判定 / 空根 exit 2 / 坏 JSON 容错）；
- ``bench_topos_playback_path`` 的 cold_start 聚合与子进程采样桩测。
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

_VENV_PY = PROJECT_ROOT / ".venv" / "bin" / "python"
PYTHON = str(_VENV_PY) if _VENV_PY.is_file() else sys.executable

TOOL_4ARM = "bench_real_2k_decode_4arm.py"
TOOL_RD = "bench_rd_scan_repro.py"
TOOL_GATE = "check_decode_budget.py"
TOOL_PLAYBACK = "bench_topos_playback_path.py"


def load_tool(module_name: str):
    """按文件路径加载 tools/ 下脚本为模块（脚本自身负责 sys.path）。"""
    spec = importlib.util.spec_from_file_location(
        module_name, TOOLS_DIR / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run_tool(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON, str(TOOLS_DIR / script), *args],
                          capture_output=True, text=True)


def write_d1_result(root: Path, head: str = "abc12345",
                    dylib8: str = "deadbeef", manifest8: str = "12345678",
                    utc: str = "20260915T051555356217Z", w: int = 3840,
                    h: int = 2160, seq_p99: tuple = (30.0,),
                    seek_p99: float | None = None,
                    drop_p99: bool = False) -> Path:
    """在 tmp 树里造一个 d1_playback 形状的 result.json。"""
    out_dir = root / head / dylib8 / manifest8 / utc
    out_dir.mkdir(parents=True, exist_ok=True)
    runs = [{"count": 96, "mean_ms": 24.0, "p50_ms": 24.0, "p95_ms": 29.0,
             "fps": 40.0} for _ in seq_p99]
    if not drop_p99:
        for run, p99 in zip(runs, seq_p99):
            run["p99_ms"] = p99
    record = {
        "manifest": {"schema": "bench_manifest.v1", "git_head": head,
                     "git_dirty": False,
                     "dylib_sha256": dylib8 + "0" * 56},
        "scope": {"boundary": "test"},
        "input": {"width": w, "height": h, "pixel_format": "yuv444p12le",
                  "bit_depth": 12, "measured_frames": 96},
        "config": {"threads": 16, "read_ahead_requested": 0,
                   "repeats": len(seq_p99)},
        "sequential_runs": runs,
        "random_seek": ({"count": 8, "p99_ms": seek_p99}
                        if seek_p99 is not None else None),
        "rss_peak_mib": 738.0,
    }
    path = out_dir / "result.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# --help


class TestHelpScreens:
    @pytest.mark.parametrize("script", [
        TOOL_4ARM, TOOL_RD, TOOL_GATE, TOOL_PLAYBACK,
    ])
    def test_help_exits_zero(self, script):
        proc = run_tool(script, "--help")
        assert proc.returncode == 0, proc.stderr
        assert "usage:" in proc.stdout

    def test_4arm_help_flags(self):
        out = run_tool(TOOL_4ARM, "--help").stdout
        for flag in ("--input-h264", "--input-topos", "--frames", "--repeats",
                     "--arms", "--interleave", "--threads", "--out-root",
                     "--allow-dirty", "--dry-run"):
            assert flag in out, flag

    def test_rd_help_flags(self):
        out = run_tool(TOOL_RD, "--help").stdout
        for flag in ("--res", "--input", "--width", "--height", "--profile",
                     "--pixel-format", "--bit-depth", "--qps", "--entropy",
                     "--frames", "--threads", "--generations", "--dry-run"):
            assert flag in out, flag

    def test_gate_help_flags(self):
        out = run_tool(TOOL_GATE, "--help").stdout
        for flag in ("--root", "--budget-ms", "--min-height"):
            assert flag in out, flag

    def test_playback_help_has_cold_start(self):
        out = run_tool(TOOL_PLAYBACK, "--help").stdout
        assert "--cold-start" in out
        # 内部探针旗标必须保持隐藏
        assert "--cold-start-probe" not in out


# ---------------------------------------------------------------------------
# --dry-run（无媒体可跑通）


class TestDryRun:
    def test_4arm_dry_run_default(self):
        proc = run_tool(TOOL_4ARM, "--dry-run")
        assert proc.returncode == 0, proc.stderr
        assert "[plan]" in proc.stdout
        assert "参数与输出路径自检通过" in proc.stdout
        for arm in ("ffmpeg", "videotoolbox", "topos_wrapper",
                    "topos_native"):
            assert arm in proc.stdout

    def test_4arm_dry_run_missing_media_warns_not_fails(self):
        proc = run_tool(TOOL_4ARM, "--dry-run",
                        "--input-h264", "/nonexistent/a.mp4",
                        "--input-topos", "/nonexistent/b.mov")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.count("WARNING") == 2

    def test_rd_dry_run_default(self):
        proc = run_tool(TOOL_RD, "--dry-run")
        assert proc.returncode == 0, proc.stderr
        assert "[plan]" in proc.stdout
        assert "case 2k" in proc.stdout and "case 4k" in proc.stdout
        assert "参数与输出路径自检通过" in proc.stdout

    def test_rd_dry_run_anchor_and_custom(self):
        proc = run_tool(TOOL_RD, "--dry-run", "--res", "2k",
                        "--qps", "45", "--entropy", "V2", "--frames", "64")
        assert proc.returncode == 0
        assert "[45]" in proc.stdout and "['V2']" in proc.stdout
        proc = run_tool(TOOL_RD, "--dry-run", "--input", "/nonexistent/x.mov",
                        "--qps", "20,25")
        assert proc.returncode == 0, proc.stderr
        assert "case custom" in proc.stdout
        assert "WARNING" in proc.stdout

    def test_4arm_rejects_bad_params(self):
        assert run_tool(TOOL_4ARM, "--dry-run", "--frames", "0").returncode != 0
        assert run_tool(TOOL_4ARM, "--dry-run", "--repeats", "0").returncode != 0
        assert run_tool(TOOL_4ARM, "--dry-run", "--warmup", "5",
                        "--frames", "5").returncode != 0
        assert run_tool(TOOL_4ARM, "--dry-run",
                        "--arms", "nope").returncode != 0

    def test_rd_rejects_bad_params(self):
        assert run_tool(TOOL_RD, "--dry-run", "--qps", "abc").returncode != 0
        assert run_tool(TOOL_RD, "--dry-run", "--entropy", "V3").returncode != 0
        assert run_tool(TOOL_RD, "--dry-run", "--frames", "0").returncode != 0
        assert run_tool(TOOL_RD, "--dry-run", "--width", "0",
                        "--input", "/nonexistent/x.mov").returncode != 0


# ---------------------------------------------------------------------------
# 4arm 单元逻辑


class TestFourArmUnits:
    @pytest.fixture(scope="class")
    def mod(self):
        return load_tool("bench_real_2k_decode_4arm")

    def test_summarize_stats(self, mod):
        stats = mod.summarize([2.0, 4.0, 6.0, 8.0], 0.01)
        assert stats["count"] == 4
        assert stats["p50_ms"] == pytest.approx(5.0)
        # 线性插值：pos = 3 * 0.99 = 2.97 → 6 + (8-6) * 0.97
        assert stats["p99_ms"] == pytest.approx(7.94)
        assert stats["mean_ms"] == pytest.approx(5.0)
        assert stats["fps"] == pytest.approx(400.0)

    def test_summarize_empty(self, mod):
        stats = mod.summarize([], 0.0)
        assert stats["count"] == 0 and stats["fps"] == 0.0

    def test_classic_comparison_isomorphic_keys(self, mod):
        arm_stats = {
            "ffmpeg": {"fps_median": 1079.0, "p50_ms_median": None},
            "videotoolbox": {"fps_median": 304.0, "p50_ms_median": None},
            "topos_wrapper": {"fps_median": 135.6, "p50_ms_median": 6.12},
            "topos_native": {"fps_median": 335.6, "p50_ms_median": 2.68},
        }
        cmp = mod.classic_comparison(arm_stats, 120)
        # 与 bench_out/real_2k_h264_20260907/result.json decode_comparison 同名
        assert cmp["h264_ffmpeg_120_frames_fps"] == 1079.0
        assert cmp["h264_videotoolbox_120_frames_fps"] == 304.0
        assert cmp["topos_wrapper_120_frames_fps"] == 135.6
        assert cmp["topos_native_120_frames_fps"] == 335.6
        assert cmp["topos_wrapper_p50_ms"] == 6.12
        assert cmp["topos_native_p50_ms"] == 2.68
        # 帧数参数化：非 120 时键名跟随
        cmp8 = mod.classic_comparison(arm_stats, 8)
        assert "h264_ffmpeg_8_frames_fps" in cmp8

    def test_aggregate_arm_median(self, mod):
        agg = mod.aggregate_arm([
            {"count": 96, "fps": 100.0, "p50_ms": 2.0},
            {"count": 96, "fps": 200.0, "p50_ms": 4.0},
            {"count": 96, "fps": 150.0, "p50_ms": 3.0},
        ])
        assert agg["fps_median"] == pytest.approx(150.0)
        assert agg["p50_ms_median"] == pytest.approx(3.0)
        assert agg["repeats"] == 3

    def test_plan_dict_reflects_args(self, mod):
        args = mod.build_arg_parser().parse_args([
            "--dry-run", "--frames", "7", "--repeats", "2",
            "--arms", "ffmpeg", "topos_native",
        ])
        plan = mod.plan_dict(args)
        assert plan["frames"] == 7 and plan["repeats"] == 2
        assert plan["arms"] == ["ffmpeg", "topos_native"]
        assert isinstance(plan["input_h264_exists"], bool)
        assert "<manifest8>" in plan["out_dir_template"]

    def test_validate_rejects(self, mod):
        bad = mod.build_arg_parser().parse_args(["--frames", "0"])
        with pytest.raises(SystemExit):
            mod.validate_args(bad)


# ---------------------------------------------------------------------------
# rd scan 单元逻辑


class TestRdScanUnits:
    @pytest.fixture(scope="class")
    def mod(self):
        return load_tool("bench_rd_scan_repro")

    def test_parse_int_list(self, mod):
        assert mod.parse_int_list("20,25,30", "qps") == [20, 25, 30]
        assert mod.parse_int_list(" 45 ", "qps") == [45]
        with pytest.raises(SystemExit):
            mod.parse_int_list("abc", "qps")
        with pytest.raises(SystemExit):
            mod.parse_int_list(",,", "qps")

    def test_parse_entropies(self, mod):
        assert mod.parse_entropies("v2,v1,V2") == ["V2", "V1"]
        with pytest.raises(SystemExit):
            mod.parse_entropies("V3")

    def test_resolve_cases_custom(self, mod):
        args = mod.build_arg_parser().parse_args([
            "--input", "/tmp/x.mov", "--width", "1920", "--height", "1080",
            "--profile", "5", "--pixel-format", "1", "--bit-depth", "12",
        ])
        cases = mod.resolve_cases(args)
        assert list(cases) == ["custom"]
        case = cases["custom"]
        assert (case["w"], case["h"]) == (1920, 1080)
        assert case["profile"] == 5 and case["pixel_format"] == 1
        assert case["bit_depth"] == 12
        assert case["src"] == "/tmp/x.mov"

    def test_resolve_cases_custom_validation(self, mod):
        base = ["--input", "/tmp/x.mov"]
        for extra in (["--width", "0"], ["--profile", "0"],
                      ["--pixel-format", "9"], ["--bit-depth", "8"]):
            # argparse choices 拒绝（SystemExit 2）与 resolve_cases 自检
            # （SystemExit msg）都属"参数被拒绝"，一并接受
            with pytest.raises(SystemExit):
                args = mod.build_arg_parser().parse_args(base + extra)
                mod.resolve_cases(args)

    def test_resolve_cases_preset(self, mod):
        args = mod.build_arg_parser().parse_args(["--res", "2k"])
        cases = mod.resolve_cases(args)
        assert cases["2k"]["w"] == 1920 and cases["2k"]["profile"] == 3

    def test_plan_dict_structure(self, mod):
        args = mod.build_arg_parser().parse_args([
            "--dry-run", "--res", "2k", "--qps", "45", "--entropy", "V2",
            "--frames", "64", "--generations", "0",
        ])
        args.qps = mod.parse_int_list(args.qps, "qps")
        args.gen_qps = mod.parse_int_list(args.gen_qps, "gen-qps")
        args.entropies = mod.parse_entropies(args.entropy)
        plan = mod.plan_dict(args)
        assert plan["qps"] == [45] and plan["entropies"] == ["V2"]
        assert "2k" in plan["cases"]
        assert "src_exists" in plan["cases"]["2k"]
        assert plan["out_dir_template"].endswith("<utc>")


# ---------------------------------------------------------------------------
# check_decode_budget 判定逻辑（tmp_path 造 result.json）


class TestDecodeBudgetGate:
    @pytest.fixture(scope="class")
    def mod(self):
        return load_tool("check_decode_budget")

    def test_within_budget_passes(self, mod, tmp_path):
        write_d1_result(tmp_path, seq_p99=(30.0, 31.0))
        summary = mod.collect(tmp_path, 33.3)
        assert summary["files"] == 1
        assert summary["checked"] == 2
        assert summary["violations"] == []
        assert mod.main(["--root", str(tmp_path)]) == 0

    def test_over_budget_fails_with_detail(self, mod, tmp_path):
        # 复刻 BENCH-0002 已知案例：4K 4444xq 第二轮 p99=36.638
        write_d1_result(tmp_path, seq_p99=(31.617, 36.638))
        summary = mod.collect(tmp_path, 33.3)
        assert len(summary["violations"]) == 1
        v = summary["violations"][0]
        assert v["run_index"] == 1
        assert v["p99_ms"] == pytest.approx(36.638)
        assert v["over_ms"] == pytest.approx(36.638 - 33.3)
        assert v["geometry"]["height"] == 2160
        report = mod.format_report(summary)
        assert "[fail]" in report and "OVER" in report and "36.638" in report
        assert mod.main(["--root", str(tmp_path)]) == 1

    def test_budget_override(self, mod, tmp_path):
        write_d1_result(tmp_path, seq_p99=(36.638,))
        assert mod.main(["--root", str(tmp_path),
                         "--budget-ms", "40"]) == 0
        summary = mod.collect(tmp_path, 40.0)
        assert summary["violations"] == []

    def test_min_height_filter(self, mod, tmp_path):
        write_d1_result(tmp_path, head="aaa00001", dylib8="aaaa0001",
                        manifest8="11111111", w=1920, h=1080,
                        seq_p99=(50.0,))
        write_d1_result(tmp_path, head="bbb00002", dylib8="bbbb0002",
                        manifest8="22222222", w=3840, h=2160,
                        seq_p99=(30.0,))
        summary = mod.collect(tmp_path, 33.3, min_height=2160)
        assert summary["skipped_by_height"] == 1
        assert summary["checked"] == 1
        assert summary["violations"] == []
        assert mod.main(["--root", str(tmp_path), "--min-height",
                         "2160"]) == 0
        # 不过滤时 1080p 的 50ms 也应被判超支
        assert mod.main(["--root", str(tmp_path)]) == 1

    def test_missing_p99_skipped_not_failed(self, mod, tmp_path):
        write_d1_result(tmp_path, seq_p99=(30.0,), drop_p99=True)
        summary = mod.collect(tmp_path, 33.3)
        assert summary["checked"] == 0
        assert summary["missing_p99"] == 1
        assert mod.main(["--root", str(tmp_path)]) == 0

    def test_random_seek_counted_not_gated(self, mod, tmp_path):
        write_d1_result(tmp_path, seq_p99=(30.0,), seek_p99=500.0)
        summary = mod.collect(tmp_path, 33.3)
        assert summary["seek_runs"] == 1
        assert summary["violations"] == []
        assert mod.main(["--root", str(tmp_path)]) == 0

    def test_empty_root_exit_2(self, mod, tmp_path):
        (tmp_path / "nothing_here").mkdir()
        assert mod.main(["--root", str(tmp_path / "nothing_here")]) == 2

    def test_missing_root_exit_2(self, mod, tmp_path):
        assert mod.main(["--root", str(tmp_path / "no_such_dir")]) == 2

    def test_bad_json_counted_not_crashed(self, mod, tmp_path):
        write_d1_result(tmp_path, seq_p99=(30.0,))
        bad = tmp_path / "zz" / "yy" / "ww" / "vv" / "tt"
        bad.mkdir(parents=True)
        (bad / "result.json").write_text("{ not json", encoding="utf-8")
        summary = mod.collect(tmp_path, 33.3)
        assert summary["parse_errors"] == 1
        assert summary["files"] == 2
        assert mod.main(["--root", str(tmp_path)]) == 0

    def test_bad_budget_rejected(self, mod, tmp_path):
        with pytest.raises(SystemExit):
            mod.main(["--root", str(tmp_path), "--budget-ms", "0"])

    def test_label_from_manifest_tree(self, mod, tmp_path):
        path = write_d1_result(tmp_path)
        record = mod.load_record(path)
        label = mod.record_label(path, record)
        assert "abc12345" in label and "12345678/20260915T051555356217Z" in label


# ---------------------------------------------------------------------------
# cold-start（BENCH-0001）


class TestColdStart:
    @pytest.fixture(scope="class")
    def mod(self):
        return load_tool("bench_topos_playback_path")

    def test_aggregate_cold_start(self, mod):
        raw = {
            "probe_argv": ["python", "probe"],
            "samples": [
                {"open_ms": 80.0, "cold_first_frame_ms": 100.0},
                {"open_ms": 150.0, "cold_first_frame_ms": 200.0},
                {"open_ms": 250.0, "cold_first_frame_ms": 300.0},
            ],
        }
        agg = mod.aggregate_cold_start(raw)
        assert agg["count"] == 3
        assert agg["cold_first_frame_ms"] == [100.0, 200.0, 300.0]
        assert agg["open_ms"] == [80.0, 150.0, 250.0]
        assert agg["p50_ms"] == pytest.approx(200.0)
        assert agg["min_ms"] == pytest.approx(100.0)
        assert agg["max_ms"] == pytest.approx(300.0)
        assert agg["mean_ms"] == pytest.approx(200.0)
        assert agg["probe_argv"] == ["python", "probe"]

    def test_spawn_cold_starts_parses_probe_json(self, mod, monkeypatch):
        payload = {"open_ms": 12.5, "first_frame_after_open_ms": 3.2,
                   "cold_first_frame_ms": 15.7, "frame_index": 0,
                   "threads": 4}

        def fake_run(argv, capture_output, text):
            return subprocess.CompletedProcess(
                argv, 0, stdout="[cold-probe] " + json.dumps(payload) + "\n",
                stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        raw = mod.spawn_cold_starts(2, Path("/x.mov"), 4, -1, -1)
        assert len(raw["samples"]) == 2
        assert raw["samples"][0]["cold_first_frame_ms"] == 15.7
        # 子进程命令：同脚本 + 探针旗标 + 输入/线程参数透传
        argv = raw["probe_argv"]
        assert argv[0] == sys.executable
        assert argv[1] == str(TOOLS_DIR / TOOL_PLAYBACK)
        assert "--cold-start-probe" in argv
        assert "--input" in argv and "/x.mov" in argv
        assert "--threads" in argv

    def test_spawn_cold_starts_child_failure_raises(self, mod, monkeypatch):
        def fake_run(argv, capture_output, text):
            return subprocess.CompletedProcess(argv, 1, stdout="",
                                               stderr="boom")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="子进程失败"):
            mod.spawn_cold_starts(1, Path("/x.mov"), 4, -1, -1)

    def test_spawn_cold_starts_missing_json_raises(self, mod, monkeypatch):
        def fake_run(argv, capture_output, text):
            return subprocess.CompletedProcess(argv, 0, stdout="garbage",
                                               stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="未输出探针 JSON"):
            mod.spawn_cold_starts(1, Path("/x.mov"), 4, -1, -1)

    def test_aggregate_empty(self, mod):
        agg = mod.aggregate_cold_start({"probe_argv": [], "samples": []})
        assert agg["count"] == 0 and agg["p50_ms"] == 0.0
