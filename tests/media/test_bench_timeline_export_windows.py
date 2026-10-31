"""bench_timeline_export_windows 纯函数与协议声明测试（计划步骤 0）。

真实导出与 GPU 测量留在 Windows 真机手动执行；本文件钉死：
- T0–T10 用例矩阵的诚实声明（未就绪用例必须有 blocking 原因）；
- 分位数 / 中位数计算；
- FFmpeg GPU 参照命令构造（同分辨率、同码率、vendor 硬编）；
- 运行记录折叠（summarize_group）与最终 JSON schema 组装；
- Markdown 报告渲染包含协议、失败披露与结论。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.bench_timeline_export_windows import (
    ALL_CASE_IDS,
    ALL_GROUP_IDS,
    BenchCase,
    D3D11_VS_FFMPEG_GATE_RATIO,
    assemble_results,
    build_input_manifest,
    build_ffmpeg_gpu_cmd,
    case_matrix,
    entry_conclusions,
    percentile,
    render_markdown,
    source_path,
    summarize_group,
)
from scripts import bench_timeline_export_windows as bench


class TestCaseMatrix:
    def test_covers_t0_to_t6(self) -> None:
        cases = case_matrix()
        assert tuple(c.case_id for c in cases) == ALL_CASE_IDS

    def test_blocked_cases_have_blocking_reason(self) -> None:
        for case in case_matrix():
            if not case.runner_ready:
                assert case.blocking, f"{case.case_id} 缺 blocking 原因"

    def test_t0_and_t1_are_ready(self) -> None:
        ready = {c.case_id for c in case_matrix() if c.runner_ready}
        assert {"T0", "T1"} <= ready

    def test_declared_sources_exist(self) -> None:
        for case in case_matrix():
            for name in case.sources:
                path = source_path(name)
                assert path.exists(), f"{case.case_id} 素材缺失: {path}"

    def test_t1_resolution_is_smaller_than_source(self) -> None:
        t1 = next(c for c in case_matrix() if c.case_id == "T1")
        assert t1.output_resolution is not None
        assert t1.output_resolution[0] < 1920

    def test_matrix_includes_d3d11_group_and_w_t10(self) -> None:
        assert ALL_CASE_IDS == tuple(f"T{i}" for i in range(11))
        assert "d3d11_gpu_surface" in ALL_GROUP_IDS
        assert bench.GROUP_ENV["cpu_pipeline"]["TOPOS_EXPORT_GPU_RENDER"] == "0"


def test_input_manifest_records_hash_and_probe(monkeypatch, tmp_path: Path) -> None:
    import scripts.bench_timeline_export_windows as bench

    source = tmp_path / "fixture.mp4"
    source.write_bytes(b"fixture-bytes")
    monkeypatch.setattr(bench, "TEST_VIDEOS_DIR", tmp_path)
    monkeypatch.setattr(
        bench,
        "probe_source",
        lambda _path: {"codec": "h264", "width": 16, "height": 16},
    )
    case = BenchCase("T0", "fixture", "test", (source.name,), True)
    manifest = build_input_manifest((case,))
    assert manifest[source.name]["status"] == "ok"
    assert manifest[source.name]["sha256"] == bench.file_sha256(source)
    assert manifest[source.name]["probe"]["codec"] == "h264"


def test_group_child_uses_isolated_config_path(monkeypatch, tmp_path: Path) -> None:
    """Benchmark setup must not touch the user's roaming Topos config."""
    import scripts.bench_timeline_export_windows as bench

    class _Capability:
        ready = False
        reason = "test_blocked"

        def to_dict(self) -> dict:
            return {"ready": False, "reason": self.reason}

    monkeypatch.setattr(
        "src.shared.media.windows_hw.d3d11_export_capability"
        ".probe_d3d11_export_capability",
        lambda: _Capability(),
    )
    result_path = tmp_path / "T0_d3d11_gpu_surface_result.json"
    assert bench.run_group_main(
        "T0", "d3d11_gpu_surface", 1, 0, 1, 8_000_000, result_path,
    ) == 0
    assert result_path.exists()
    assert Path(bench.os.environ["TOPOS_CONFIG_PATH"]) == (
        tmp_path / "T0_d3d11_gpu_surface_config.json"
    )


class TestPercentile:
    def test_single_sample(self) -> None:
        assert percentile([5.0], 0.5) == 5.0

    def test_linear_interpolation(self) -> None:
        samples = [10.0, 20.0, 30.0, 40.0]
        assert percentile(samples, 0.5) == 25.0
        assert percentile(samples, 0.0) == 10.0
        assert percentile(samples, 1.0) == 40.0

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError):
            percentile([], 0.5)

    def test_median(self) -> None:
        from scripts.bench_timeline_export_windows import median

        assert median([3.0, 1.0, 2.0]) == 2.0


class TestFfmpegRefCommand:
    def test_amd_gpu_surface_command(self) -> None:
        cmd = build_ffmpeg_gpu_cmd(
            "ffmpeg", Path("in.mp4"), Path("out.mp4"),
            frames=480, bitrate_bps=20_000_000, vendor="amd",
            gpu_surface=True,
        )
        joined = " ".join(cmd)
        assert "-hwaccel d3d11va" in joined
        assert "-hwaccel_output_format d3d11" in joined
        assert "-c:v h264_amf" in joined
        assert "-frames:v 480" in joined
        assert "-b:v 20000000" in joined
        assert "-an" in joined
        assert "-progress pipe:1" in joined

    def test_nvidia_vendor_maps_to_nvenc(self) -> None:
        cmd = build_ffmpeg_gpu_cmd(
            "ffmpeg", Path("in.mp4"), Path("out.mp4"),
            frames=10, bitrate_bps=1_000_000, vendor="nvidia",
            gpu_surface=False,
        )
        joined = " ".join(cmd)
        assert "-hwaccel cuda" in joined
        assert "-c:v h264_nvenc" in joined
        assert "hwaccel_output_format" not in joined

    def test_encoder_options_are_vendor_specific(self) -> None:
        """AMF 的 -quality/-rc vbr_peak 对 h264_nvenc 无效，必须按 vendor 区分。"""
        nvenc = build_ffmpeg_gpu_cmd(
            "ffmpeg", Path("in.mp4"), Path("out.mp4"),
            frames=10, bitrate_bps=1_000_000, vendor="nvidia",
            gpu_surface=False,
        )
        joined = nvenc
        assert "-preset" in joined and joined[joined.index("-preset") + 1] == "p5"
        assert joined[joined.index("-rc") + 1] == "vbr"
        assert "-quality" not in joined

        amf = build_ffmpeg_gpu_cmd(
            "ffmpeg", Path("in.mp4"), Path("out.mp4"),
            frames=10, bitrate_bps=1_000_000, vendor="amd",
            gpu_surface=False,
        )
        assert amf[amf.index("-quality") + 1] == "quality"
        assert amf[amf.index("-rc") + 1] == "vbr_peak"

    def test_download_mode_omits_output_format(self) -> None:
        cmd = build_ffmpeg_gpu_cmd(
            "ffmpeg", Path("in.mp4"), Path("out.mp4"),
            frames=10, bitrate_bps=1_000_000, vendor="amd",
            gpu_surface=False,
        )
        assert "hwaccel_output_format" not in " ".join(cmd)

    def test_t1_uses_gpu_resize_for_d3d11_reference(self) -> None:
        cmd = build_ffmpeg_gpu_cmd(
            "ffmpeg", Path("in.mp4"), Path("out.mp4"),
            frames=30, bitrate_bps=20_000_000, vendor="amd",
            gpu_surface=True, output_resolution=(1280, 720),
        )
        joined = " ".join(cmd)
        assert "-vf scale_d3d11=1280:720" in joined
        assert " scale=1280:720" not in joined
        assert cmd.index("-i") < cmd.index("-vf")

    def test_invalid_output_resolution_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_ffmpeg_gpu_cmd(
                "ffmpeg", Path("in.mp4"), Path("out.mp4"),
                frames=30, bitrate_bps=20_000_000, vendor="amd",
                gpu_surface=True, output_resolution=(0, 720),
            )


class TestSummarizeGroup:
    def _payload(self) -> dict:
        return {
            "status": "ok",
            "runs": [
                {
                    "run_index": 1, "status": "ok", "frames": 480,
                    "fps": 100.0, "frame_ms_p50": 9.5, "frame_ms_p95": 12.0,
                    "output_mb": 30.0, "output_sha256": "aaa",
                    "probe": {"codec": "h264", "decoded_frames": 480},
                },
                {
                    "run_index": 2, "status": "ok", "frames": 480,
                    "fps": 90.0, "frame_ms_p50": 10.5, "frame_ms_p95": 14.0,
                    "output_mb": 32.0, "output_sha256": "bbb",
                    "probe": {"codec": "h264", "decoded_frames": 480},
                },
                {
                    "run_index": 3, "status": "ok", "frames": 480,
                    "fps": 110.0, "frame_ms_p50": 8.5, "frame_ms_p95": 13.0,
                    "output_mb": 31.0, "output_sha256": "aaa",
                    "probe": {"codec": "h264", "decoded_frames": 480},
                },
            ],
        }

    def test_median_and_frame_times(self) -> None:
        summary = summarize_group(self._payload())
        assert summary["status"] == "ok"
        assert summary["runs_ok"] == 3
        assert summary["median_fps"] == 100.0
        assert summary["median_frame_ms_p50"] == 9.5
        assert summary["max_frame_ms_p95"] == 14.0

    def test_sha_stability_flag(self) -> None:
        summary = summarize_group(self._payload())
        assert summary["output_sha256_stable"] is False
        payload = self._payload()
        payload["runs"][1]["output_sha256"] = "aaa"
        summary = summarize_group(payload)
        assert summary["output_sha256_stable"] is True

    def test_all_failed_group_reports_error(self) -> None:
        payload = {
            "status": "error",
            "runs": [{"run_index": 1, "status": "failed", "error": "boom"}],
        }
        summary = summarize_group(payload)
        assert summary["status"] == "error"
        assert summary["error"] == "boom"
        assert "median_fps" not in summary

    def test_declared_group_reports_blocking_reason(self) -> None:
        summary = summarize_group({
            "status": "declared_not_run",
            "blocking": "not_yet_implemented:native_bridge",
        })
        assert summary["status"] == "declared_not_run"
        assert summary["error"] == "not_yet_implemented:native_bridge"

    def test_ffmpeg_reference_fallback_reason_is_preserved(self) -> None:
        summary = summarize_group({
            "status": "ok",
            "ref_mode_fallback": True,
            "ref_mode_fallback_reason": "scale_d3d11: E_INVALIDARG",
            "runs": [{
                "status": "ok", "fps": 60.0, "output_mb": 1.0,
                "output_sha256": "x", "probe": {},
            }],
        })
        assert summary["ref_mode_fallback_reason"] == (
            "scale_d3d11: E_INVALIDARG"
        )


class TestAssembleResults:
    def test_declared_case_passes_through(self) -> None:
        results = assemble_results(
            meta={"date": "2026-09-15"},
            protocol={"frames": 480},
            case_results={
                "T2": {
                    "title": "内建效果代表组合",
                    "status": "declared_not_run",
                    "blocking": "效果接线批次",
                },
            },
        )
        assert results["schema_version"] == 1
        assert results["cases"]["T2"]["blocking"] == "效果接线批次"

    def test_baseline_ratios_computed_against_ffmpeg(self) -> None:
        results = assemble_results(
            meta={},
            protocol={},
            case_results={
                "T0": {
                    "status": "ok",
                    "groups": {
                        "cpu_pipeline": {"status": "ok", "runs": [
                            {"status": "ok", "fps": 50.0, "output_mb": 1.0,
                             "output_sha256": "x", "probe": {}},
                        ]},
                        "hw_download": {"status": "ok", "runs": [
                            {"status": "ok", "fps": 100.0, "output_mb": 1.0,
                             "output_sha256": "x", "probe": {}},
                        ]},
                        "ffmpeg_gpu_ref": {"status": "ok", "runs": [
                            {"status": "ok", "fps": 200.0, "output_mb": 1.0,
                             "output_sha256": "x", "probe": {}},
                        ]},
                    },
                },
            },
        )
        ratios = results["cases"]["T0"]["baseline_ratios"]
        assert ratios["cpu_pipeline_vs_ffmpeg"] == 0.25
        assert ratios["hw_download_vs_ffmpeg"] == 0.5

    def test_gate_constant_matches_plan(self) -> None:
        """计划 §4.3：T0/T1 ≥ FFmpeg GPU 基线的 85%。"""
        assert D3D11_VS_FFMPEG_GATE_RATIO == 0.85


class TestMarkdownRendering:
    def test_renders_protocol_failures_and_conclusions(self) -> None:
        results = assemble_results(
            meta={"date": "2026-09-15", "gpu_tag": "amd_rx6800",
                  "json_name": "windows_timeline_export_amd_rx6800_2026-09-15.json"},
            protocol={"frames": 480, "warmup_frames": 60, "runs": 3},
            case_results={
                "T0": {
                    "title": "单段 H.264 直通", "status": "ok",
                    "groups": {
                        "hw_download": {"status": "ok", "runs": [
                            {"status": "ok", "fps": 100.0, "output_mb": 30.0,
                             "output_sha256": "x", "probe": {}},
                        ]},
                        "ffmpeg_gpu_ref": {"status": "error", "runs": [
                            {"status": "failed", "error": "amf init failed"},
                        ]},
                    },
                    "baseline_ratios": {},
                },
                "T2": {"title": "效果", "status": "declared_not_run",
                       "blocking": "效果接线批次"},
            },
        )
        md = render_markdown(results)
        assert "# Windows 时间线导出基准（amd_rx6800）" in md
        assert "输出 480 帧" in md
        assert "declared_not_run" in md
        assert "效果接线批次" in md
        # 失败组必须在结论里诚实披露，不得粉饰
        assert "ffmpeg_gpu_ref 失败" in md
        assert "## 结论" in md

    def test_conclusions_mention_ratio_baseline(self) -> None:
        results = assemble_results(
            meta={},
            protocol={},
            case_results={
                "T0": {
                    "title": "直通", "status": "ok",
                    "groups": {},
                    "baseline_ratios": {"hw_download_vs_ffmpeg": 0.62},
                },
            },
        )
        items = entry_conclusions(results)
        assert any("62.0%" in item for item in items)


class TestCliContract:
    def test_unknown_case_rejected_by_parser(self) -> None:
        from scripts.bench_timeline_export_windows import main

        with pytest.raises(SystemExit):
            main(["--cases", "T11", "--dry-run"])

    def test_dry_run_prints_plan_without_execution(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        from scripts.bench_timeline_export_windows import main

        rc = main(["--cases", "T0,T2", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "T0" in out and "READY" in out
        assert "T2" in out and "BLOCKED" in out


class TestSubprocessGroupContract:
    def _case(self) -> BenchCase:
        return next(c for c in case_matrix() if c.case_id == "T0")

    def test_keep_outputs_flag_is_forwarded_to_child(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """--keep-outputs 必须传给子进程，否则子进程测量后照常删除 mp4。"""
        recorded_cmds: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            recorded_cmds.append(list(cmd))
            emit = Path(cmd[cmd.index("--emit-json") + 1])
            emit.write_text('{"status": "ok"}', encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(bench.subprocess, "run", fake_run)
        payload = bench.run_group_in_subprocess(
            self._case(), "hw_download", 1, 0, 1, 8_000_000,
            tmp_path, 60.0, keep_outputs=True,
        )
        assert payload["status"] == "ok"
        assert "--keep-outputs" in recorded_cmds[0]

        payload = bench.run_group_in_subprocess(
            self._case(), "hw_download", 1, 0, 1, 8_000_000,
            tmp_path, 60.0, keep_outputs=False,
        )
        assert payload["status"] == "ok"
        assert "--keep-outputs" not in recorded_cmds[1]

    def test_child_timeout_returns_error_payload_instead_of_raising(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """子进程超时必须落结构化 error（计划 §8），父进程不允许未处理异常。"""
        def fake_run(_cmd, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="child", timeout=0.01)

        monkeypatch.setattr(bench.subprocess, "run", fake_run)
        payload = bench.run_group_in_subprocess(
            self._case(), "hw_download", 1, 0, 1, 8_000_000,
            tmp_path, 0.01,
        )
        assert payload["status"] == "error"
        assert "超时" in payload["error"]


class TestFfmpegRefFallbackProtocol:
    def test_gpu_surface_fallback_redoes_warmup_before_formal_runs(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """降级 hwaccel_download 后必须先重跑预热：正式 run 不允许冷启动。"""
        case = next(c for c in case_matrix() if c.case_id == "T0")
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            Path(cmd[-1]).write_bytes(b"out")
            # 首跑（gpu_surface 预热）失败 → 触发降级；其余全部成功。
            ok = len(calls) > 1
            return SimpleNamespace(returncode=0 if ok else 1,
                                   stderr="scale_d3d11 E_INVALIDARG", stdout="")

        monkeypatch.setattr(bench.subprocess, "run", fake_run)
        monkeypatch.setattr(bench, "_ffmpeg_bin", lambda: "ffmpeg")
        monkeypatch.setattr(bench, "probe_source", lambda _p: {"total_frames": 9999})
        monkeypatch.setattr(bench, "probe_output", lambda _p: {})
        monkeypatch.setattr(
            "src.shared.export.video_encoder.detect_hw_vendor", lambda: "amd",
        )
        payload = bench.run_ffmpeg_ref_group(
            case, frames=2, warmup_frames=1, runs=2, bitrate_bps=8_000_000,
            workdir=tmp_path, timeout_s=60.0,
        )
        # 1 次失败的 gpu_surface 预热 + 1 次降级预热 + 2 次正式 run。
        assert len(calls) == 4
        assert "-hwaccel_output_format" in " ".join(calls[0])
        assert all("-hwaccel_output_format" not in " ".join(c) for c in calls[1:])
        assert payload["ref_mode"] == "hwaccel_download"
        assert "redone" in payload["warmup"]
        ok_runs = [r for r in payload["runs"] if r["status"] == "ok"]
        assert len(ok_runs) == 2
        assert {r["run_index"] for r in ok_runs} == {1, 2}
