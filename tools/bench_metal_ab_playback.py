#!/usr/bin/env python3
"""Topos side of the Metal / OpenGL A/B playback benchmark.

Migration plan phase 0.4 and appendix A measure Edit playback in three backend
configurations.  Each configuration is declared here explicitly so a run
cannot silently mix environments, and the driver bounds the work:
``topos_bench.py playback --repeat N`` repeats the whole case matrix ``N``
times (the appendix's ``601`` style labels are export labels, not counts).

For every configuration the driver runs ``N`` rounds over the benchmark
timelines and aggregates, per timeline, how many of the 400 expected frames
were actually presented (``frame_presented``), plus the decode / render path
distribution and CPU transfer bytes reported by the playback trace.

The benchmark opens the real application window maximized; keep it visible and
do not run other load during a measurement.  With the display asleep the same
build presents a handful of frames per case instead of ~400 (measured: first
case 398 frames, later cases 5–17), so the driver refuses to measure on a
sleeping display and keeps an awake display from dozing off mid-run.

Run from the repository root::

    PYTHONPATH=. .venv/bin/python tools/bench_metal_ab_playback.py \
        --ab-dir bench_out/metal-ab.2026-11-23 --repeats 4 \
        --output-json docs/macos_metal_migration/evidence/ab-playback.json \
        --output-md docs/macos_metal_migration/evidence/ab-playback.md
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

# The tools directory is not a package; share the helpers of the export driver
# through the repository root (``PYTHONPATH=.`` or an explicit path insert).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.bench_metal_ab_topos import ROOT, bench_env, machine_facts, prepare_ab_dir  # noqa: E402

PLAYBACK_TIMEOUT_S = 3600.0
EXPECTED_FRAMES = 400
# After the last case the application only has to tear down and exit.
EXIT_GRACE_S = 90.0


@dataclass(frozen=True)
class PlaybackConfig:
    """One backend configuration: a tag plus the environment that selects it."""

    tag: str
    env: Mapping[str, str]


# ``auto`` uses the production rollout. ``metal`` isolates Metal rendering
# with the compatible presentation path, and ``native`` enables eager attach.
CONFIGS: Dict[str, PlaybackConfig] = {
    "gl": PlaybackConfig("gl", {"TOPOS_BACKEND_GRAPHICS": "opengl",
                                "TOPOS_EDIT_METAL_PREVIEW": "0"}),
    "auto": PlaybackConfig("auto", {"TOPOS_BACKEND_GRAPHICS": "auto"}),
    "metal": PlaybackConfig("metal", {"TOPOS_BACKEND_GRAPHICS": "metal",
                                      "TOPOS_EDIT_METAL_VIEWPORT": "0"}),
    # The native CAMetalLayer viewport (plan 2.1-2.3): Metal frames go straight
    # to the layer instead of through the IOSurface -> GL slot detour.
    "native": PlaybackConfig("native", {"TOPOS_BACKEND_GRAPHICS": "metal",
                                        "TOPOS_EDIT_METAL_VIEWPORT": "1"}),
}

TRACE_COUNTERS = (
    "stale_frame_emitted",
    "texture_slot_reuse_without_ack",
    "cpu_fallback_frames",
    "qimage_upload_frames",
    "gl_tex_image_upload_frames",
    "ui_thread_sync_decode_frames",
    "sync_seek_frames",
)
TRACE_DISTRIBUTIONS = (
    "decode_path_counts",
    "render_path_counts",
    "present_status_counts",
    "readback_kind_counts",
    "fallback_reasons",
    "backend_counts",
    "present_path_counts",
)


def display_power_state() -> Optional[Tuple[int, int]]:
    """``(current, max)`` IODisplayWrangler power state; ``None`` if unknown.

    A sleeping display reports a state below its maximum.  The numbers are
    only comparable between displays that report the same wrangler, which is
    why the raw pair is recorded with each report.
    """
    try:
        completed = subprocess.run(
            ["ioreg", "-r", "-n", "IODisplayWrangler"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    current = re.search(r'"CurrentPowerState"=(\d+)', completed.stdout)
    maximum = re.search(r'"MaxPowerState"=(\d+)', completed.stdout)
    if not current or not maximum:
        return None
    return int(current.group(1)), int(maximum.group(1))


def start_display_keep_awake(pid: int) -> Optional["subprocess.Popen[bytes]"]:
    """Hold the display awake while ``pid`` runs (never wakes a sleeping one)."""
    if shutil.which("caffeinate") is None:
        return None
    try:
        return subprocess.Popen(
            ["caffeinate", "-d", "-i", "-w", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def rotate_previous_records(ab_dir: Path) -> None:
    """Keep any earlier playback records instead of deleting them."""
    jsonl = ab_dir / "topos_playback.jsonl"
    if jsonl.exists() and jsonl.stat().st_size:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        jsonl.rename(ab_dir / f"topos_playback.{stamp}.jsonl")


def clean_stale_session_files(ab_dir: Path) -> List[str]:
    """Remove recovery files a killed benchmark session left for its own project.

    A ``.topos.autosave`` newer than the project makes the application show a
    modal recovery prompt at launch; the benchmark timers keep running behind
    it with no project loaded (0-frame cases) and the application never
    exits.  Only the benchmark project's files inside the A/B directory are
    touched — never a user project.
    """
    removed: List[str] = []
    for project in sorted(ab_dir.glob("*.topos")):
        for leftover in (
            ab_dir / f"{project.name}.autosave",
            ab_dir / f".{project.name}.autosave.lock",
        ):
            try:
                if leftover.exists():
                    leftover.unlink()
                    removed.append(leftover.name)
            except OSError:
                continue
    return removed


def run_config(
    ab_dir: Path, config: PlaybackConfig, repeats: int, *, stall_timeout_s: float
) -> List[Dict[str, Any]]:
    """Run one configuration for ``repeats`` rounds and tag its records."""
    jsonl = ab_dir / "topos_playback.jsonl"
    jsonl.write_text("", encoding="utf-8")
    env = bench_env(ab_dir, {})
    env.update(config.env)
    log_path = ab_dir / "logs" / f"playback_{config.tag}.log"
    cases = repeats * 2 * 2  # sources x graded flag
    with log_path.open("w", encoding="utf-8") as log:
        # Stream to the log file so a stalled session can be inspected live.
        process = subprocess.Popen(
            [sys.executable, str(ab_dir / "topos_bench.py"),
             "playback", "--repeat", str(repeats)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
        )
        keep_awake = start_display_keep_awake(process.pid)
        try:
            outcome = _wait_for_cases(process, jsonl, cases, stall_timeout_s)
        finally:
            if process.poll() is None:
                _terminate(process)
            if keep_awake is not None and keep_awake.poll() is None:
                keep_awake.terminate()
                keep_awake.wait(timeout=10.0)
            clean_stale_session_files(ab_dir)
    if outcome.stalled:
        raise RuntimeError(
            f"playback benchmark '{config.tag}' stalled (no new case for "
            f"{stall_timeout_s:g}s, {outcome.completed}/{cases} cases); "
            f"terminated — see {log_path}"
        )
    if outcome.returncode not in (None, 0):
        raise RuntimeError(
            f"playback benchmark '{config.tag}' exited {outcome.returncode}; see {log_path}"
        )
    rows: List[Dict[str, Any]] = []
    starts = parse_play_lines(log_path)
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        key = (record.get("source"), bool(record.get("graded")), int(record.get("repeat") or 0))
        row = tag_record(record, config, starts.get(key))
        row["app_exit"] = outcome.app_exit
        rows.append(row)
    # The bench script writes the raw records without the backend tag; keep a
    # tagged copy next to them so later aggregation never has to guess.
    (ab_dir / f"topos_playback.{config.tag}.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    return rows


def _count_records(jsonl: Path) -> int:
    try:
        return sum(1 for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


@dataclass(frozen=True)
class _Outcome:
    """How a benchmark session ended."""

    returncode: Optional[int]
    completed: int
    stalled: bool
    # "exited": the application quit by itself; "terminated_after_cases": every
    # case was measured but the application did not exit and was stopped.
    app_exit: str


def _wait_for_cases(
    process: "subprocess.Popen[str]", jsonl: Path, cases: int, stall_timeout_s: float
) -> _Outcome:
    """Wait for the benchmark, stopping it when no case completes in time.

    All cases measured but no exit is acceptable: the session is stopped and
    its records are kept.  A session that stops producing cases before the
    matrix is complete is a stall and its records are rejected.
    """
    seen = _count_records(jsonl)
    last_progress = time.monotonic()
    deadline = last_progress + PLAYBACK_TIMEOUT_S
    while True:
        returncode = process.poll()
        if returncode is not None:
            return _Outcome(returncode, seen, False, "exited")
        time.sleep(1.0)
        current = _count_records(jsonl)
        if current != seen:
            seen, last_progress = current, time.monotonic()
            continue
        # Every case has been measured: the application only has to exit.
        # Give it a bounded grace period instead of a full stall timeout.
        if seen >= cases:
            if time.monotonic() - last_progress > EXIT_GRACE_S:
                _terminate(process)
                return _Outcome(None, seen, False, "terminated_after_cases")
            continue
        if time.monotonic() - last_progress > stall_timeout_s or time.monotonic() > deadline:
            return _Outcome(None, seen, True, "terminated_stalled")


def _terminate(process: "subprocess.Popen[str]") -> None:
    """Stop a benchmark process that did not exit on its own."""
    process.terminate()
    try:
        process.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10.0)


_PLAY_LINE = re.compile(
    r"^PLAY \('(?P<source>[^']+)', (?P<graded>True|False), (?P<round>\d+)\) "
    r"(?P<start>-?\d+) (?P<playing>True|False) (?P<visible>True|False) (?P<total>-?\d+)\s*$"
)


def parse_play_lines(log_path: Path) -> Dict[tuple, Dict[str, Any]]:
    """Read the bench's ``PLAY`` lines: timeline length and playback state.

    The record written per case does not include the timeline length; the
    trace's ``total_frames`` counts traced frames, not timeline frames.  The
    ``PLAY`` line is the only place the harness reports the real length.
    """
    starts: Dict[tuple, Dict[str, Any]] = {}
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return starts
    for line in lines:
        match = _PLAY_LINE.match(line)
        if match:
            starts[(match["source"], match["graded"] == "True", int(match["round"]))] = {
                "timeline_frames": int(match["total"]),
                "playing_at_start": match["playing"] == "True",
                "viewer_visible": match["visible"] == "True",
            }
    return starts


def tag_record(
    record: Dict[str, Any],
    config: PlaybackConfig,
    start: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Flatten one bench record into the fields the report needs."""
    trace = record.get("trace") or {}
    frames = record.get("frames") or []
    pixel_bytes = trace.get("pixel_copy_bytes") or {}
    wall_s = float(record.get("wall_s") or 0.0)
    if start is not None:
        # The harness reported the timeline length before playback started.
        valid = (
            start["timeline_frames"] == EXPECTED_FRAMES
            and start["playing_at_start"]
            and len(frames) > 0
        )
    else:
        # Imported raw record without its log: a loaded timeline plays for
        # several seconds; an unloaded one stops after the 1 s floor.
        valid = len(frames) > 0 and wall_s > 2.0
    row: Dict[str, Any] = {
        "config": config.tag,
        "env": dict(config.env),
        "source": record.get("source"),
        "graded": bool(record.get("graded")),
        "round": record.get("repeat"),
        "wall_s": record.get("wall_s"),
        "presented_frames": len({int(frame[1]) for frame in frames}),
        "last_frame": frames[-1][1] if frames else None,
        "timeline_frames": start["timeline_frames"] if start else None,
        # A case is only comparable when the timeline loaded (400 frames),
        # playback started and at least one frame reached the screen.
        "valid": valid,
        "cpu_upload_bytes": pixel_bytes.get("cpu_upload"),
        "cpu_download_bytes": pixel_bytes.get("cpu_download"),
        "surface_map_bytes": pixel_bytes.get("surface_map"),
    }
    for key in TRACE_COUNTERS:
        row[key] = trace.get(key)
    for key in TRACE_DISTRIBUTIONS:
        row[key] = trace.get(key)
    return row


def merge_counts(target: Dict[str, int], source: Optional[Mapping[str, Any]]) -> None:
    """Sum one distribution into ``target``."""
    for key, value in (source or {}).items():
        try:
            target[key] = target.get(key, 0) + int(value)
        except (TypeError, ValueError):
            continue


def summarize(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate rounds per configuration and timeline."""
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["config"], row["source"], row["graded"]), []).append(row)
    summary: List[Dict[str, Any]] = []
    for (tag, source, graded), all_members in sorted(groups.items()):
        # Invalid rounds (timeline not loaded, nothing presented) measure the
        # harness, not the backend; keep them visible but out of the numbers.
        members = [m for m in all_members if m.get("valid", True)]
        invalid = [m for m in all_members if not m.get("valid", True)]
        if not members:
            summary.append({
                "config": tag, "source": source, "graded": graded,
                "rounds": 0, "invalid_rounds": len(invalid),
                "presented_per_round": [], "presented_median": None, "presented_min": None,
                "presented_max": None, "dropped_median": None, "wall_median_s": None,
                "backend_counts": {},
            })
            continue
        counts = {key: 0 for key in TRACE_COUNTERS}
        distributions = {key: {} for key in TRACE_DISTRIBUTIONS}
        for member in members:
            for key in TRACE_COUNTERS:
                try:
                    counts[key] += int(member.get(key) or 0)
                except (TypeError, ValueError):
                    continue
            for key in TRACE_DISTRIBUTIONS:
                merge_counts(distributions[key], member.get(key))
        presented = [int(m["presented_frames"]) for m in members]
        walls = [float(m["wall_s"]) for m in members if m.get("wall_s") is not None]
        summary.append({
            "config": tag,
            "source": source,
            "graded": graded,
            "rounds": len(members),
            "invalid_rounds": len(invalid),
            "presented_per_round": presented,
            "presented_median": statistics.median(presented),
            "presented_min": min(presented),
            "presented_max": max(presented),
            "dropped_median": EXPECTED_FRAMES - statistics.median(presented),
            "wall_median_s": round(statistics.median(walls), 3) if walls else None,
            "decode_path_counts": distributions["decode_path_counts"],
            "render_path_counts": distributions["render_path_counts"],
            "present_status_counts": distributions["present_status_counts"],
            "fallback_reasons": distributions["fallback_reasons"],
            "readback_kind_counts": distributions["readback_kind_counts"],
            "backend_counts": distributions["backend_counts"],
            "present_path_counts": distributions["present_path_counts"],
            "max_cpu_upload_bytes": max((m.get("cpu_upload_bytes") or 0) for m in members),
            "max_cpu_download_bytes": max((m.get("cpu_download_bytes") or 0) for m in members),
            **counts,
        })
    return summary


def render_markdown(report: Dict[str, Any]) -> str:
    """Render the playback report as one table per configuration."""
    lines = [
        "# Topos Edit 播放基线（Metal / OpenGL A/B）",
        "",
        f"- 采集时间（UTC）: `{report['generated_at_utc']}`",
        f"- 提交: `{report['commit']}`",
        f"- 机器: {report['machine'].get('hostname', '?')} · "
        f"{report['machine'].get('cpu', '?')} · {', '.join(report['machine'].get('gpus', []) or ['?'])}",
        f"- A/B 目录: `{report['ab_dir']}` · 各配置轮数见「各轮呈现帧」 · "
        f"分母为区间唯一期望帧 {EXPECTED_FRAMES}",
        f"- 显示器电源状态（采集时）: {report.get('display_power_state') or '未记录'}"
        + ("（**测量时显示器休眠，不可与清醒基线比较**）"
           if (report.get("display_power_state") or [1, 1])[0]
           < (report.get('display_power_state') or [1, 1])[1] else ""),
        *(f"- {note}" for note in report.get("notes") or []),
        "",
        "呈现帧数按 `frame_presented` 的帧号去重。OpenGL 取绘制事件，Metal 取 drawable 显示回调。中位数跨有效轮次。"
        "时间线未加载或 0 帧的用例计为无效轮，不进入统计。",
        "",
        "| 配置 | 环境变量 | 时间线 | 呈现帧中位数 | 各轮呈现帧 | 无效轮 | 丢帧中位数 | "
        "实际渲染后端 | 实际呈现面 | 解码路径 | 旧帧覆盖 | 槽位未确认复用 | CPU 上传/回读字节 |",
        "|---|---|---|---:|---|---:|---:|---|---|---|---:|---:|---:|",
    ]
    for row in report["summary"]:
        env = " ".join(f"{k}={v}" for k, v in row["env"].items())
        rounds = ", ".join(str(v) for v in row["presented_per_round"])
        if row["presented_median"] is None:
            lines.append(
                f"| `{row['config']}` | `{env}` | "
                f"{row['source']} {'LUT' if row['graded'] else 'Original'} | — | — | "
                f"{row['invalid_rounds']} | — | — | — | — | — | — | — | — |"
            )
            continue
        lines.append(
            f"| `{row['config']}` | `{env}` | {row['source']} {'LUT' if row['graded'] else 'Original'} | "
            f"{row['presented_median']:g} | {rounds} | {row['invalid_rounds']} | "
            f"{row['dropped_median']:g} | "
            f"`{json.dumps(row['backend_counts'], ensure_ascii=False)}` | "
            f"`{json.dumps(row.get('present_path_counts') or {}, ensure_ascii=False)}` | "
            f"`{json.dumps(row['decode_path_counts'], ensure_ascii=False)}` | "
            f"{row['stale_frame_emitted']} | {row['texture_slot_reuse_without_ack']} | "
            f"{row['max_cpu_upload_bytes']}/{row['max_cpu_download_bytes']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ab-dir", type=Path, required=True,
                        help="isolated A/B directory holding the benchmark project")
    parser.add_argument("--configs", default="gl,auto,metal",
                        help="comma-separated configuration tags")
    parser.add_argument("--repeats", type=int, default=4,
                        help="rounds over the case matrix per configuration (default 4)")
    parser.add_argument("--stall-timeout", type=float, default=180.0,
                        help="seconds without a completed case before the session is "
                             "stopped (default 180)")
    parser.add_argument("--allow-display-asleep", action="store_true",
                        help="measure even though the display is asleep (numbers are "
                             "not comparable with an awake-display baseline)")
    parser.add_argument("--import", dest="imports", nargs="*", metavar="TAG=JSONL", default=[],
                        help="reuse records already measured for a configuration instead of "
                             "running it (raw or tagged topos_playback records)")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    try:
        configs = [CONFIGS[tag.strip()] for tag in args.configs.split(",") if tag.strip()]
    except KeyError as exc:
        parser.error(f"unknown configuration {exc}; choose from {', '.join(CONFIGS)}")
    imported: Dict[str, Path] = {}
    for item in args.imports:
        tag, sep, path = item.partition("=")
        if not sep or tag not in CONFIGS:
            parser.error(f"--import expects TAG=JSONL with a tag from {', '.join(CONFIGS)}")
        imported[tag] = Path(path)
        if not any(item.tag == tag for item in configs):
            configs.append(CONFIGS[tag])

    ab_dir = (ROOT / args.ab_dir) if not args.ab_dir.is_absolute() else args.ab_dir
    prepare_ab_dir(ab_dir, build=False, extra_env={})
    if not (ab_dir / "Topos_UHD25_AB.topos").exists():
        print(f"benchmark project missing in {ab_dir}; run bench_metal_ab_topos.py --build first",
              file=sys.stderr)
        return 2
    rotate_previous_records(ab_dir)
    stale = clean_stale_session_files(ab_dir)
    if stale:
        print(f"removed stale benchmark session files: {stale}", flush=True)

    power = display_power_state()
    if power is not None:
        print(f"display power state: {power[0]}/{power[1]}", flush=True)
    will_measure = any(config.tag not in imported for config in configs)
    if not will_measure:
        # Every record is imported: the display state now says nothing about
        # the conditions the records were measured under.
        power = None
    if (will_measure and power is not None and power[0] < power[1]
            and not args.allow_display_asleep):
        print(
            f"display is asleep (power state {power[0]}/{power[1]}); a sleeping display "
            "presents only a few frames per case, so the numbers would not be "
            "comparable. Wake the display, or pass --allow-display-asleep to record "
            "an explicitly non-comparable run.",
            file=sys.stderr, flush=True,
        )
        return 3

    rows: List[Dict[str, Any]] = []
    for config in configs:
        if config.tag in imported:
            # Reuse the session's own log when it is still there, so imported
            # records get the authoritative timeline-length check.
            starts = parse_play_lines(ab_dir / "logs" / f"playback_{config.tag}.log")
            for line in imported[config.tag].read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    if "config" in record:
                        rows.append(record)
                        continue
                    key = (record.get("source"), bool(record.get("graded")),
                           int(record.get("repeat") or 0))
                    rows.append(tag_record(record, config, starts.get(key)))
            print(f"[{config.tag}] imported {sum(1 for r in rows if r['config'] == config.tag)} "
                  f"record(s) from {imported[config.tag]}", flush=True)
            continue
        print(f"[{config.tag}] {args.repeats} round(s)", flush=True)
        rows.extend(run_config(
            ab_dir, config, args.repeats, stall_timeout_s=args.stall_timeout))
        for row in (r for r in rows if r["config"] == config.tag):
            print(
                f"    {row['source']} {'LUT' if row['graded'] else 'Original'} round {row['round']}: "
                f"presented {row['presented_frames']}/{EXPECTED_FRAMES} "
                f"{'ok' if row['valid'] else 'INVALID'} "
                f"decode={row['decode_path_counts']}",
                flush=True,
            )

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    summary = summarize(rows)
    report: Dict[str, Any] = {
        "schema": "topos_metal_ab_playback/1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": commit,
        "machine": machine_facts(),
        "ab_dir": str(ab_dir.relative_to(ROOT)) if ab_dir.is_relative_to(ROOT) else str(ab_dir),
        "repeats": args.repeats,
        "expected_frames": EXPECTED_FRAMES,
        # Recorded so a report can never be read as an awake-display baseline
        # by accident.
        "display_power_state": list(power) if power is not None else None,
        "display_asleep_allowed": bool(args.allow_display_asleep),
        "summary": summary,
        "runs": rows,
    }
    for row in summary:
        row["env"] = dict(CONFIGS[row["config"]].env)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(render_markdown(report), encoding="utf-8")
    for row in summary:
        print(
            f"{row['config']:5s} {row['source']:5s} {'LUT' if row['graded'] else 'Orig'}: "
            f"presented median {row['presented_median']}/{EXPECTED_FRAMES} "
            f"backend={row.get('backend_counts')}",
            flush=True,
        )
    invalid = [r for r in rows if not r.get("valid", True)]
    if invalid:
        print(
            f"INVALID rounds (timeline not loaded or 0 frames presented): "
            f"{[(r['config'], r['source'], r['graded'], r['round']) for r in invalid]}",
            file=sys.stderr, flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
