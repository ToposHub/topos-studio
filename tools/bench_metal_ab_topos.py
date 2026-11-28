#!/usr/bin/env python3
"""Topos side of the Metal / OpenGL A/B export benchmark.

Migration plan phase 0.4 and appendix A run the existing ``topos_bench.py``
fixture by hand.  This driver automates the same commands so every phase can
re-measure the baseline the same way:

- prepare an isolated A/B directory (script + LUT copies, separate config),
- run each export path for one first run plus N warm runs, alternating the
  path order between rounds,
- verify the first-run outputs with ``verify_exports.py``,
- aggregate wall time, actual backend and CPU transfer bytes into one JSON and
  one Markdown report.

The driver never edits the fixture; it only launches the benchmark copy.
Script paths stay relative to the repository root so the fixture keeps
locating media through ``OUT.parents[1]``.

Run from the repository root::

    PYTHONPATH=. .venv/bin/python tools/bench_metal_ab_topos.py \
        --ab-dir bench_out/metal-ab.2026-11-23 --rounds 6 \
        --output-json docs/architecture/macos_metal_migration/evidence/ab-export.json \
        --output-md docs/architecture/macos_metal_migration/evidence/ab-export.md
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "bench_out" / "resolve_vs_topos_2026-11-22"
FIXTURE_FILES = (
    "topos_bench.py",
    "verify_exports.py",
    "resolve_bench.py",
    "Topos_Resolve_AB_33.cube",
)
EXPORT_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class ExportPath:
    """One benchmark export path: bench arguments plus backend environment."""

    key: str
    label_base: int
    source: str
    codec: str
    graded: bool
    env: Mapping[str, str]
    expected_delivery_path: str = ""

    def repeat_label(self, round_index: int) -> int:
        """Round 0 keeps the appendix A labels (101 / 201 / 301 / 401)."""
        return self.label_base + 1 + round_index

    def export_name(self, round_index: int) -> str:
        mode = "LUT" if self.graded else "Original"
        return f"Topos_{self.source}_{mode}_to_{self.codec}_{self.repeat_label(round_index)}"


# Mirrors appendix A. ``metal_dnxhr`` is the "same scene, to be measured" row of
# the plan's starting table and is opt-in.
PATHS: Dict[str, ExportPath] = {
    "gl_h264": ExportPath(
        "gl_h264", 100, "h264", "h264", True,
        {"TOPOS_BACKEND_GRAPHICS": "opengl", "TOPOS_EXPORT_NATIVE_VT": "0"},
    ),
    "metal_h264": ExportPath(
        "metal_h264", 200, "h264", "h264", True,
        {"TOPOS_BACKEND_GRAPHICS": "metal"},
        expected_delivery_path="metal_h264_nv12_lut",
    ),
    "gl_dnxhr": ExportPath(
        "gl_dnxhr", 300, "dnxhr", "dnxhr", True,
        {"TOPOS_BACKEND_GRAPHICS": "opengl"},
    ),
    "metal_dnxhr": ExportPath(
        "metal_dnxhr", 400, "dnxhr", "dnxhr", True,
        {"TOPOS_BACKEND_GRAPHICS": "metal"},
    ),
}


def prepare_ab_dir(ab_dir: Path, *, build: bool, extra_env: Mapping[str, str]) -> None:
    """Create or refresh the isolated A/B directory and optionally rebuild."""
    ab_dir.mkdir(parents=True, exist_ok=True)
    # Everything under the A/B directory is regenerable evidence (media is
    # large); keep it out of git and copy small results into docs instead.
    ignore = ab_dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    for name in FIXTURE_FILES:
        src = FIXTURE / name
        if not src.is_file():
            raise FileNotFoundError(f"benchmark fixture missing: {src}")
        dst = ab_dir / name
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            dst.write_bytes(src.read_bytes())
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True)
    (ab_dir / "commit.txt").write_text(commit, encoding="utf-8")
    (ab_dir / "logs").mkdir(exist_ok=True)
    if build:
        run = subprocess.run(
            [sys.executable, str(ab_dir / "topos_bench.py"), "build"],
            cwd=ROOT, env=bench_env(ab_dir, extra_env), timeout=EXPORT_TIMEOUT_S,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        (ab_dir / "logs" / "build.log").write_text(run.stdout, encoding="utf-8")
        if run.returncode != 0:
            raise RuntimeError(f"topos_bench.py build failed; see {ab_dir / 'logs' / 'build.log'}")


def bench_env(ab_dir: Path, extra_env: Mapping[str, str]) -> Dict[str, str]:
    """Environment for one benchmark process (isolated config, repo on path)."""
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(ROOT) if not existing_pythonpath else str(ROOT) + os.pathsep + existing_pythonpath
    )
    env["TOPOS_CONFIG_PATH"] = str(ab_dir / "topos_runtime" / "config.json")
    env.update(extra_env)
    return env


def run_export(
    ab_dir: Path, spec: ExportPath, round_index: int, extra_env: Mapping[str, str]
) -> Dict[str, Any]:
    """Run one export and return the record appended to ``topos_exports.jsonl``."""
    jsonl = ab_dir / "topos_exports.jsonl"
    before = jsonl.read_text(encoding="utf-8").count("\n") if jsonl.exists() else 0
    env = bench_env(ab_dir, extra_env)
    env.update(spec.env)
    log_path = ab_dir / "logs" / f"{spec.key}_r{spec.repeat_label(round_index)}.log"
    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable, str(ab_dir / "topos_bench.py"), "export",
            "--source", spec.source, "--codec", spec.codec,
            *(["--graded"] if spec.graded else []),
            "--repeat", str(spec.repeat_label(round_index)),
        ],
        cwd=ROOT, env=env, timeout=EXPORT_TIMEOUT_S,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    log_path.write_text(completed.stdout, encoding="utf-8")
    record: Dict[str, Any] = {
        "path": spec.key,
        "round": round_index,
        "name": spec.export_name(round_index),
        "process_wall_s": round(time.monotonic() - started, 3),
        "returncode": completed.returncode,
        "success": False,
        "log": str(log_path.relative_to(ROOT)),
    }
    if not jsonl.exists():
        record["error"] = "topos_exports.jsonl missing"
        return record
    lines = jsonl.read_text(encoding="utf-8").splitlines()
    for line in lines[before:]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("name") == record["name"]:
            record.update(summarize_record(data))
            break
    else:
        record["error"] = "no result record for this run"
    return record


def summarize_record(data: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten one benchmark record into the fields the report needs."""
    telemetry = data.get("stage_telemetry") or {}
    stages: List[Dict[str, Any]] = []
    for stage in telemetry.get("stages") or []:
        stages.append({
            "name": stage.get("name"),
            "backend": stage.get("backend"),
            "sample_count": stage.get("sample_count"),
            "wall_p50_ms": stage.get("wall_p50_ms"),
            "wall_p95_ms": stage.get("wall_p95_ms"),
            "gpu_submit_ms": stage.get("gpu_submit_ms"),
            "cpu_upload_bytes": stage.get("cpu_upload_bytes"),
            "cpu_download_bytes": stage.get("cpu_download_bytes"),
            "fallback_reason": stage.get("fallback_reason"),
        })
    return {
        "success": bool(data.get("success")),
        "error": data.get("error") or "",
        "wall_s": data.get("wall_s"),
        "delivery_path": telemetry.get("delivery_path") or "",
        "full_gpu": bool(telemetry.get("full_gpu")),
        "full_gpu_blockers": list(telemetry.get("full_gpu_blockers") or []),
        "fallback_root_cause": telemetry.get("fallback_root_cause"),
        "cpu_upload_bytes": telemetry.get("cpu_upload_bytes"),
        "cpu_download_bytes": telemetry.get("cpu_download_bytes"),
        "stages": stages,
    }


def run_verify(ab_dir: Path, files: Sequence[Path], extra_env: Mapping[str, str]) -> List[Dict[str, Any]]:
    """Verify export containers and return the per-file verification rows."""
    if not files:
        return []
    completed = subprocess.run(
        [sys.executable, str(ab_dir / "verify_exports.py"), *(str(f) for f in files)],
        cwd=ROOT, env=bench_env(ab_dir, extra_env), timeout=EXPORT_TIMEOUT_S,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    (ab_dir / "logs" / "verify.log").write_text(completed.stdout, encoding="utf-8")
    report = ab_dir / "export_verification.json"
    if not report.exists():
        return []
    return list(json.loads(report.read_text(encoding="utf-8")))


def summarize_path(spec: ExportPath, runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """First run, warm-run spread and backend facts for one export path."""
    first = runs[0]
    warm = [r for r in runs[1:] if r.get("success") and r.get("wall_s") is not None]
    warm_walls = [float(r["wall_s"]) for r in warm]
    stage_rows: Dict[str, Dict[str, Any]] = {}
    for run in warm or runs[:1]:
        for stage in run.get("stages") or []:
            row = stage_rows.setdefault(stage["name"], {
                "name": stage["name"], "backends": set(), "sample_count": stage["sample_count"],
                "wall_p50_ms": [], "wall_p95_ms": [], "cpu_upload_bytes": 0, "cpu_download_bytes": 0,
            })
            row["backends"].add(stage["backend"])
            row["sample_count"] = max(row["sample_count"] or 0, stage["sample_count"] or 0)
            if stage["wall_p50_ms"] is not None:
                row["wall_p50_ms"].append(float(stage["wall_p50_ms"]))
            if stage["wall_p95_ms"] is not None:
                row["wall_p95_ms"].append(float(stage["wall_p95_ms"]))
            row["cpu_upload_bytes"] = max(row["cpu_upload_bytes"], stage["cpu_upload_bytes"] or 0)
            row["cpu_download_bytes"] = max(row["cpu_download_bytes"], stage["cpu_download_bytes"] or 0)
    for row in stage_rows.values():
        row["backends"] = sorted(row["backends"])
        row["wall_p50_ms_median"] = statistics.median(row["wall_p50_ms"]) if row["wall_p50_ms"] else None
        row["wall_p95_ms_median"] = statistics.median(row["wall_p95_ms"]) if row["wall_p95_ms"] else None
        del row["wall_p50_ms"], row["wall_p95_ms"]
    return {
        "path": spec.key,
        "env": dict(spec.env),
        "first_run": {k: v for k, v in first.items() if k != "stages"},
        "warm_runs": len(warm),
        "warm_all_success": bool(warm) and all(r["success"] for r in runs[1:]),
        "warm_median_s": round(statistics.median(warm_walls), 3) if warm_walls else None,
        "warm_min_s": round(min(warm_walls), 3) if warm_walls else None,
        "warm_max_s": round(max(warm_walls), 3) if warm_walls else None,
        "warm_walls_s": [round(v, 3) for v in warm_walls],
        "delivery_paths": sorted({r.get("delivery_path", "") for r in runs if r.get("success")}),
        "expected_delivery_path": spec.expected_delivery_path,
        "backend_matches_expectation": all(
            not r.get("success") or r.get("delivery_path", "") == spec.expected_delivery_path
            for r in runs
        ),
        "full_gpu": sorted({bool(r.get("full_gpu")) for r in runs if r.get("success")}),
        "max_cpu_upload_bytes": max((r.get("cpu_upload_bytes") or 0) for r in runs),
        "max_cpu_download_bytes": max((r.get("cpu_download_bytes") or 0) for r in runs),
        "stages": [stage_rows[name] for name in sorted(stage_rows)],
    }


def machine_facts() -> Dict[str, Any]:
    """Static machine facts recorded with every report (no GPU context)."""
    facts: Dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
    }
    try:
        facts["hostname"] = subprocess.check_output(
            ["scutil", "--get", "ComputerName"], text=True, timeout=10
        ).strip()
        facts["cpu"] = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True, timeout=10
        ).strip()
        profiler = subprocess.check_output(
            ["system_profiler", "SPDisplaysDataType"], text=True, timeout=60
        )
        facts["gpus"] = [
            line.strip().rstrip(":") for line in profiler.splitlines()
            if line.strip().endswith(":") and "Chipset Model" not in line
            and not line.startswith("Graphics")
            and line.strip() not in ("Displays:",)
        ]
    except (OSError, subprocess.SubprocessError) as exc:
        facts["probe_error"] = str(exc)
    return facts


def bit_rate_mbps(value: Any) -> str:
    """ffprobe reports ``bit_rate`` as a decimal string; render it in Mbit/s."""
    try:
        return f"{int(value) / 1e6:.2f}"
    except (TypeError, ValueError):
        return "—"


def render_markdown(report: Dict[str, Any]) -> str:
    """Render the aggregate report as one Markdown table per export path."""
    lines = [
        "# Topos Metal / OpenGL A/B 导出基线",
        "",
        f"- 采集时间（UTC）: `{report['generated_at_utc']}`",
        f"- 提交: `{report['commit']}`",
        f"- 机器: {report['machine'].get('hostname', '?')} · "
        f"{report['machine'].get('cpu', '?')} · {', '.join(report['machine'].get('gpus', []) or ['?'])}",
        f"- Python {report['machine'].get('python')} · {report['machine'].get('platform')}",
        f"- A/B 目录: `{report['ab_dir']}` · 每条路径 {report['rounds']} 轮"
        "（第 1 轮为首跑，其后为暖跑，逐轮交替顺序）",
        "",
        "## 墙钟时间（提交任务到文件关闭）",
        "",
        "| 路径 | 环境变量 | 首跑 (s) | 暖跑中位数 (s) | 暖跑 min–max (s) | 实际交付路径 | "
        "全 GPU | CPU 上传/回读字节 | 成功 |",
        "|---|---|---:|---:|---|---|---|---:|---|",
    ]
    for row in report["paths"]:
        env = " ".join(f"{k}={v}" for k, v in row["env"].items())
        first = row["first_run"].get("wall_s")
        success = (
            f"{sum(1 for r in report['runs'] if r['path'] == row['path'] and r.get('success'))}"
            f"/{sum(1 for r in report['runs'] if r['path'] == row['path'])}"
        )
        lines.append(
            f"| `{row['path']}` | `{env}` | {round(first, 3) if first is not None else '—'} | "
            f"{row['warm_median_s'] if row['warm_median_s'] is not None else '—'} | "
            f"{row['warm_min_s'] if row['warm_min_s'] is not None else '—'}–"
            f"{row['warm_max_s'] if row['warm_max_s'] is not None else '—'} | "
            f"`{', '.join(p or '(generic)' for p in row['delivery_paths']) or '—'}` | "
            f"{all(row['full_gpu']) if row['full_gpu'] else '—'} | "
            f"{row['max_cpu_upload_bytes']}/{row['max_cpu_download_bytes']} | {success} |"
        )
    lines += ["", "逐轮暖跑秒数：", ""]
    for row in report["paths"]:
        walls = ", ".join(f"{v:g}" for v in row["warm_walls_s"])
        lines.append(f"- `{row['path']}`: {walls}")
    lines += ["", "## 分阶段（暖跑中位数，毫秒）", ""]
    for row in report["paths"]:
        lines += [
            f"### `{row['path']}`", "",
            "| 阶段 | 后端 | 样本数 | 墙钟 p50 | 墙钟 p95 | CPU 上传字节 | CPU 回读字节 |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for stage in row["stages"]:
            if not stage["sample_count"]:
                continue
            p50 = stage["wall_p50_ms_median"]
            p95 = stage["wall_p95_ms_median"]
            lines.append(
                f"| {stage['name']} | `{', '.join(str(b) for b in stage['backends'])}` | "
                f"{stage['sample_count']} | {round(p50, 3) if p50 is not None else '—'} | "
                f"{round(p95, 3) if p95 is not None else '—'} | {stage['cpu_upload_bytes']} | "
                f"{stage['cpu_download_bytes']} |"
            )
        lines.append("")
    verification = report.get("verification") or []
    if verification:
        lines += ["## 成片核验（首跑产物）", "",
                  "| 文件 | 帧数 | 编码 | 像素格式 | 尺寸 | 帧率 | 实际码率 (Mbit/s) | "
                  "编码器 | range / matrix / transfer / primaries | 格式合格 |",
                  "|---|---:|---|---|---|---|---:|---|---|---|"]
        for item in verification:
            lines.append(
                f"| `{item['path']}` | {item['decoded_frames']} | {item['codec_name']} | "
                f"{item['pix_fmt']} | {item['width']}×{item['height']} | {item['avg_frame_rate']} | "
                f"{bit_rate_mbps(item.get('bit_rate'))} | {item.get('encoder') or '—'} | "
                f"{item.get('color_range')} / {item.get('color_space')} / "
                f"{item.get('color_transfer')} / {item.get('color_primaries')} | "
                f"{'✅' if item['format_ok'] else '❌'} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def parse_env(values: Optional[Sequence[str]]) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` pairs from the command line."""
    parsed: Dict[str, str] = {}
    for value in values or ():
        key, sep, val = value.partition("=")
        if not sep or not key:
            raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {value!r}")
        parsed[key] = val
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ab-dir", type=Path, required=True,
                        help="isolated A/B directory (created if missing; git-ignored)")
    parser.add_argument("--paths", default="gl_h264,metal_h264,gl_dnxhr",
                        help="comma-separated export path keys (default appendix A set)")
    parser.add_argument("--rounds", type=int, default=6,
                        help="runs per path including the first run (default 6)")
    parser.add_argument("--build", action="store_true", help="rebuild the benchmark project first")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip verify_exports.py on the first-run outputs")
    parser.add_argument("--env", nargs="*", metavar="KEY=VALUE", default=[],
                        help="extra environment applied to every benchmark process")
    parser.add_argument("--output-json", type=Path, help="aggregate report (JSON)")
    parser.add_argument("--output-md", type=Path, help="aggregate report (Markdown)")
    parser.add_argument("--render-from-json", type=Path,
                        help="skip the runs and re-render Markdown from an existing JSON report")
    args = parser.parse_args()
    if args.render_from_json is not None:
        report = json.loads(args.render_from_json.read_text(encoding="utf-8"))
        target = args.output_md or args.render_from_json.with_suffix(".md")
        target.write_text(render_markdown(report), encoding="utf-8")
        print(f"wrote {target}", flush=True)
        return 0
    if args.rounds < 1:
        parser.error("--rounds must be >= 1")
    try:
        specs = [PATHS[key.strip()] for key in args.paths.split(",") if key.strip()]
    except KeyError as exc:
        parser.error(f"unknown path {exc}; choose from {', '.join(PATHS)}")
    extra_env = parse_env(args.env)

    ab_dir = (ROOT / args.ab_dir) if not args.ab_dir.is_absolute() else args.ab_dir
    prepare_ab_dir(ab_dir, build=args.build, extra_env=extra_env)

    runs: List[Dict[str, Any]] = []
    for round_index in range(args.rounds):
        # Alternate the order so a warm cache or thermal drift does not
        # always favour the same path.
        order = specs if round_index % 2 == 0 else list(reversed(specs))
        for spec in order:
            print(f"[{round_index + 1}/{args.rounds}] {spec.key}", flush=True)
            run = run_export(ab_dir, spec, round_index, extra_env)
            runs.append(run)
            print(
                f"    wall={run.get('wall_s')} delivery_path={run.get('delivery_path')!r} "
                f"success={run.get('success')} {run.get('error', '')}".rstrip(),
                flush=True,
            )

    verification: List[Dict[str, Any]] = []
    if not args.no_verify:
        first_outputs = [
            ab_dir / f"{spec.export_name(0)}{'.mp4' if spec.codec == 'h264' else '.mov'}"
            for spec in specs
        ]
        verification = run_verify(
            ab_dir, [path for path in first_outputs if path.exists()], extra_env
        )

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    report: Dict[str, Any] = {
        "schema": "topos_metal_ab_export/1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": commit,
        "machine": machine_facts(),
        "ab_dir": str(ab_dir.relative_to(ROOT)) if ab_dir.is_relative_to(ROOT) else str(ab_dir),
        "rounds": args.rounds,
        "extra_env": extra_env,
        "paths": [
            summarize_path(spec, [r for r in runs if r["path"] == spec.key]) for spec in specs
        ],
        "runs": runs,
        "verification": verification,
    }
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(render_markdown(report), encoding="utf-8")

    failed = [r for r in runs if not r.get("success")]
    for row in report["paths"]:
        print(
            f"{row['path']}: first={row['first_run'].get('wall_s')}s "
            f"warm_median={row['warm_median_s']}s delivery_path={row['delivery_paths']}",
            flush=True,
        )
    if failed:
        print(f"FAILED runs: {[r['name'] for r in failed]}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
