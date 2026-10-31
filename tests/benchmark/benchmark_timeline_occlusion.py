"""
Timeline 遮挡裁剪实机基准（计划 §7：docs/timeline_occlusion_culling_optimization_plan_2026-09-19.md）。

使用 tests/test_videos 真实素材，对 B0–B6 基准组计时并输出 telemetry
证据（实际减少的 layer 取帧请求，而非仅 Python 循环次数）。

用法：
    # 全部基准组，默认 预热60帧/统计480帧/3轮取中位数（计划 §7 口径）
    .venv/bin/python tests/benchmark/benchmark_timeline_occlusion.py

    # 快速抽检
    .venv/bin/python tests/benchmark/benchmark_timeline_occlusion.py --warmup 8 --measure 48 --runs 1

BENCH-0015（2026-09-29 追加）：

- ``--runs N`` 控制 ON/OFF 交替轮数（历史结果 runs=1/2 无 CI，4K B1 的
  3.67× 只能表述为观察值）。每组结果新增 ``speedup_ci95``（成对百分位
  bootstrap，统计量 = median(off)/median(on)；runs<2 时为 null）。
  runs<3 时打印 WARNING 并在 payload 写 ``runs_warning``——bootstrap CI
  在如此小的样本下不稳定，正式复测应 runs>=3。
- **剔除组生效断言**：每组记录 ``occlusion_activated``（cull_on 侧
  culled_ratio>0）。若一次运行的所有组都未激活剔除（历史 2K 版只有 B0
  单轨组即此情形），打印 WARNING 并写 ``warning_no_active_cull_group``，
  整份结果不构成剔除证据；``--require-cull`` 可把该情形升级为 exit 1
  （门禁用法）。

输出：每组 "cull=ON" vs "cull=OFF" 的渲染 wall time 中位数、
layer 取帧请求总数、culled_ratio 与 reason 分布；结果同时落 JSON。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from src.features.edit.core import timeline_occlusion as oc  # noqa: E402
from src.features.edit.core.clip_params import TransformParams  # noqa: E402
from src.features.edit.core.project import EditProject, MediaAsset  # noqa: E402
from src.features.edit.core.timeline_export import (  # noqa: E402
    ExportCodec,
    ExportSource,
    TimelineExportConfig,
    create_timeline_exporter,
)
from src.features.edit.core.timeline_render_plan import TimelineRenderPlan  # noqa: E402
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
    TransitionInstance,
)

TEST_VIDEOS = REPO / "tests" / "test_videos"

#: 素材档位（计划 §7-B5：4K 原片 vs 2K）。--material 切换。
MATERIALS = {
    "2k": {
        "lower": "2K_h264.mp4", "upper": "2K_prores422HQ.mov",
        "w": 1920, "h": 1080,
        "lower_fmt": "yuv420p", "upper_fmt": "yuv422p10le",
    },
    "4k": {
        "lower": "4K_H264.mov", "upper": "4K_prores422HQ.mov",
        "w": 3840, "h": 2160,
        "lower_fmt": "yuv420p", "upper_fmt": "yuv422p10le",
    },
}

# 当前档位（set_material 切换；默认 2k 保持旧行为）。
_MATERIAL = MATERIALS["2k"]
FPS = 25


def set_material(name: str) -> None:
    global _MATERIAL
    if name not in MATERIALS:
        raise SystemExit(f"unknown material '{name}': {sorted(MATERIALS)}")
    _MATERIAL = MATERIALS[name]


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _asset(asset_id: str, filename: str, pixel_format: str) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        path=str(TEST_VIDEOS / filename),
        media_type="video",
        display_name=asset_id,
        metadata={
            "codec": "prores" if filename.endswith(".mov") else "h264",
            "pixel_format": pixel_format,
            "width": _MATERIAL["w"], "height": _MATERIAL["h"],
            "fps_num": 25, "fps_den": 1, "fps": float(FPS),
            "total_frames": 300,
        },
    )


def _clip(clip_id, asset_id, track_id, start=0, **kw) -> ClipInstance:
    # clip 覆盖素材全长：保证测量窗口内每帧都有内容（否则空帧早退
    # 会让计时失真，请求数也不反映真实合成负载）。
    return ClipInstance(
        clip_id=clip_id, asset_id=asset_id, track_id=track_id,
        timeline_start=start, timeline_duration=300,
        source_start=start, source_duration=300, **kw,
    )


def _track(index: int, clips) -> EditTrack:
    return EditTrack(track_id=f"v{index + 1}", name=f"V{index + 1}",
                     track_type="video", index=index, clips=tuple(clips))


def _sequence(tracks) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="bench", name="OcclusionBench", fps=Rational(25, 1),
        width=_MATERIAL["w"], height=_MATERIAL["h"],
        video_tracks=tuple(tracks),
    )


# ── 基准组定义（计划 §7 表） ──


def build_b0():
    """B0 单轨：不应因分析器造成可测回归。"""
    a = _asset("a", _MATERIAL["lower"], _MATERIAL["lower_fmt"])
    project = EditProject.create(name="b0").with_media_asset_added(a)
    return project, _sequence([_track(0, [_clip("c_a", "a", "v1")])])


def build_b1():
    """B1 两轨，上轨全屏不透明：最大收益组。"""
    lower = _asset("lower", _MATERIAL["lower"], _MATERIAL["lower_fmt"])
    upper = _asset("upper", _MATERIAL["upper"], _MATERIAL["upper_fmt"])
    project = (EditProject.create(name="b1")
               .with_media_asset_added(lower).with_media_asset_added(upper))
    return project, _sequence([
        _track(0, [_clip("c_lower", "lower", "v1")]),
        _track(1, [_clip("c_upper", "upper", "v2")]),
    ])


def build_b2():
    """B2 两轨，上轨缩小 50%：验证不误裁。"""
    lower = _asset("lower", _MATERIAL["lower"], _MATERIAL["lower_fmt"])
    upper = _asset("upper", _MATERIAL["upper"], _MATERIAL["upper_fmt"])
    project = (EditProject.create(name="b2")
               .with_media_asset_added(lower).with_media_asset_added(upper))
    return project, _sequence([
        _track(0, [_clip("c_lower", "lower", "v1")]),
        _track(1, [_clip("c_upper", "upper", "v2",
                         transform=TransformParams(scale_x=0.5, scale_y=0.5))]),
    ])


def build_b3():
    """B3 四轨，最上层全屏不透明：下面三轨均不解码。"""
    lower = _asset("lower", _MATERIAL["lower"], _MATERIAL["lower_fmt"])
    upper = _asset("upper", _MATERIAL["upper"], _MATERIAL["upper_fmt"])
    project = (EditProject.create(name="b3")
               .with_media_asset_added(lower).with_media_asset_added(upper))
    return project, _sequence([
        _track(0, [_clip("c_l1", "lower", "v1")]),
        _track(1, [_clip("c_l2", "lower", "v2")]),
        _track(2, [_clip("c_l3", "lower", "v3")]),
        _track(3, [_clip("c_top", "upper", "v4")]),
    ])


def build_b4():
    """B4 两轨 + 上轨 opacity=0.5：只在安全帧裁剪，决策随帧正确。"""
    lower = _asset("lower", _MATERIAL["lower"], _MATERIAL["lower_fmt"])
    upper = _asset("upper", _MATERIAL["upper"], _MATERIAL["upper_fmt"])
    project = (EditProject.create(name="b4")
               .with_media_asset_added(lower).with_media_asset_added(upper))
    return project, _sequence([
        _track(0, [_clip("c_lower", "lower", "v1")]),
        _track(1, [_clip("c_upper", "upper", "v2", opacity=0.5)]),
    ])


BENCH_GROUPS = {
    "B0_single_track": build_b0,
    "B1_two_track_full_occlusion": build_b1,
    "B2_two_track_scaled_upper": build_b2,
    "B3_four_track_full_occlusion": build_b3,
    "B4_two_track_transparent_upper": build_b4,
}


def _make_renderer(project, seq):
    exporter = create_timeline_exporter(project, seq)
    exporter._render_plan = TimelineRenderPlan(seq)
    calls = {"n": 0}
    real_get = exporter._get_layer_frame

    def counting_get(layer):
        calls["n"] += 1
        return real_get(layer)

    exporter._get_layer_frame = counting_get
    return exporter, calls


def _config() -> TimelineExportConfig:
    return TimelineExportConfig(
        source=ExportSource.TIMELINE, timeline_id="bench", output_path="",
        codec=ExportCodec.H264, pixel_format="yuv420p",
        resolution=(_MATERIAL["w"], _MATERIAL["h"]), fps=25.0,
        include_audio=False, burn_subtitles=False,
    )


def _run_once(project, seq, config, *, warmup, measure, total_frames, cull):
    """单轮：预热 + 正式计时；返回 (elapsed_s, layer_frame_requests, culled_ratio)。"""
    oc._ENV_CACHE["TOPOS_TIMELINE_OCCLUSION_CULL"] = cull
    oc.get_occlusion_telemetry().reset()
    exporter, calls = _make_renderer(project, seq)
    for i in range(warmup):
        exporter._render_frame_impl(i % total_frames, config)
    t0 = time.perf_counter()
    for i in range(measure):
        exporter._render_frame_impl((warmup + i) % total_frames, config)
    elapsed = time.perf_counter() - t0
    snap = oc.get_occlusion_telemetry().snapshot()
    return elapsed, calls["n"], snap.get("culled_ratio", 0.0)


def bootstrap_ratio_ci(paired: list[tuple[float, float]],
                       n_boot: int = 2000, seed: int = 0xC0FFEE,
                       confidence: float = 0.95) -> dict | None:
    """成对百分位 bootstrap CI（BENCH-0015；纯函数供单测）。

    ``paired`` 为逐轮成对样本 [(on, off), ...]（run_group 轮内 ON/OFF 交错，
    同轮两臂共享机器窗口，按轮索引配对重采样）。统计量
    ``median(off) / median(on)``（= speedup）；返回百分位 CI。
    样本 < 2 对、含非正数或退化分布时返回 None。
    """
    if len(paired) < 2:
        return None
    ons = [float(a) for a, _ in paired]
    offs = [float(b) for _, b in paired]
    if any(v <= 0 for v in ons) or any(v <= 0 for v in offs):
        return None
    rng = random.Random(seed)
    stats: list[float] = []
    for _ in range(n_boot):
        idx = [rng.randrange(len(paired)) for _ in range(len(paired))]
        on_m = statistics.median(ons[i] for i in idx)
        off_m = statistics.median(offs[i] for i in idx)
        if on_m > 0:
            stats.append(off_m / on_m)
    if not stats:
        return None
    alpha = (1.0 - confidence) / 2.0
    stats.sort()

    def _pct(data: list[float], q: float) -> float:
        pos = (len(data) - 1) * q
        lo = int(pos // 1)
        hi = min(len(data) - 1, lo + 1)
        frac = pos - lo
        return data[lo] * (1 - frac) + data[hi] * frac

    return {
        "ci_low": _pct(stats, alpha),
        "ci_high": _pct(stats, 1.0 - alpha),
        "n_boot": n_boot,
        "confidence": confidence,
        "method": "paired percentile bootstrap over interleaved runs; "
                  "statistic = median(off_s)/median(on_s)",
    }


def run_group(name: str, build, *, warmup: int, measure: int, runs: int) -> dict:
    project, seq = build()
    total_frames = 300
    config = _config()
    # ON/OFF 逐轮交错执行，消除先跑一方的磁盘/解码缓存冷启动偏差
    # （偶数轮先 ON，奇数轮先 OFF）。
    samples = {True: {"s": [], "req": [], "ratio": []},
               False: {"s": [], "req": [], "ratio": []}}
    for run_idx in range(runs):
        order = (True, False) if run_idx % 2 == 0 else (False, True)
        for cull in order:
            elapsed, requests, ratio = _run_once(
                project, seq, config,
                warmup=warmup, measure=measure, total_frames=total_frames,
                cull=cull,
            )
            samples[cull]["s"].append(elapsed)
            samples[cull]["req"].append(requests)
            samples[cull]["ratio"].append(ratio)

    result = {"group": name, "cull_on": {}, "cull_off": {}}
    for cull, key in ((True, "cull_on"), (False, "cull_off")):
        s = samples[cull]
        median_s = statistics.median(s["s"])
        result[key] = {
            "median_s": median_s,
            "fps": measure / median_s,
            "layer_frame_requests_median": statistics.median(s["req"]),
            "culled_ratio": s["ratio"][-1],
            "all_runs_s": s["s"],
            "all_runs_requests": s["req"],
        }
    on = result["cull_on"]
    off = result["cull_off"]
    result["speedup"] = (
        off["median_s"] / on["median_s"] if on["median_s"] > 0 else 0.0)
    result["request_reduction"] = (
        1.0 - on["layer_frame_requests_median"] / off["layer_frame_requests_median"]
        if off["layer_frame_requests_median"] else 0.0)
    # BENCH-0015：speedup 的成对 bootstrap CI（runs<2 → null）；
    # 剔除组生效断言 —— cull_on 侧 culled_ratio>0 才算激活。
    paired = list(zip(samples[True]["s"], samples[False]["s"]))
    result["speedup_ci95"] = bootstrap_ratio_ci(paired)
    result["occlusion_activated"] = bool(
        samples[True]["ratio"] and samples[True]["ratio"][-1] > 0.0)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmup", type=int, default=60)
    parser.add_argument("--measure", type=int, default=480)
    parser.add_argument("--runs", type=int, default=3,
                        help="ON/OFF 交替轮数（BENCH-0015：正式复测 "
                             "runs>=3；<3 打 WARNING 且 CI 不稳定）")
    parser.add_argument("--groups", nargs="*", default=list(BENCH_GROUPS))
    parser.add_argument("--material", default="2k", choices=sorted(MATERIALS),
                        help="素材档位（§7-B5：4k 原片 vs 2k）")
    parser.add_argument("--require-cull", action="store_true",
                        help="没有任何组的剔除生效时 exit 1（BENCH-0015 "
                             "门禁：例如 2K 素材只跑 B0 单轨组）")
    parser.add_argument("--output", default="",
                        help="JSON 结果落盘路径（默认 benchmarks/ 下）")
    args = parser.parse_args()
    if args.runs <= 0:
        raise SystemExit("runs 必须 >= 1")
    if args.warmup < 0 or args.measure <= 0:
        raise SystemExit("warmup/measure 参数非法")

    set_material(args.material)

    runs_warning = None
    if args.runs < 3:
        runs_warning = (f"runs={args.runs} < 3：无稳定 bootstrap CI，speedup "
                        "只能表述为观察值（BENCH-0015；历史 2K runs=1 / "
                        "4K runs=2 即此口径）")
        print(f"WARNING: {runs_warning}")

    lower_sha = _sha1(TEST_VIDEOS / _MATERIAL["lower"])
    upper_sha = _sha1(TEST_VIDEOS / _MATERIAL["upper"])
    print(f"machine=darwin material={_MATERIAL['lower']}(sha1:{lower_sha}) "
          f"+ {_MATERIAL['upper']}(sha1:{upper_sha}) proxy=master "
          f"resolution={_MATERIAL['w']}x{_MATERIAL['h']} fps={FPS} backend=cpu-composite "
          f"warmup={args.warmup} measure={args.measure} runs={args.runs}")

    results = []
    for name in args.groups:
        build = BENCH_GROUPS.get(name)
        if build is None:
            print(f"skip unknown group {name}")
            continue
        print(f"--- {name} ---")
        result = run_group(
            name, build,
            warmup=args.warmup, measure=args.measure, runs=args.runs,
        )
        results.append(result)
        on = result["cull_on"]
        off = result["cull_off"]
        ci = result.get("speedup_ci95")
        ci_str = (f" CI95=[{ci['ci_low']:.2f},{ci['ci_high']:.2f}]"
                  if ci else " CI95=None(<2 runs)")
        print(
            f"  OFF: {off['median_s']:.2f}s ({off['fps']:.1f} fps) "
            f"requests={off['layer_frame_requests_median']:.0f} "
            f"culled_ratio={off['culled_ratio']:.2f}"
        )
        print(
            f"  ON : {on['median_s']:.2f}s ({on['fps']:.1f} fps) "
            f"requests={on['layer_frame_requests_median']:.0f} "
            f"culled_ratio={on['culled_ratio']:.2f}"
        )
        print(
            f"  => speedup={result['speedup']:.2f}x{ci_str} "
            f"request_reduction={result['request_reduction'] * 100:.1f}% "
            f"occlusion_activated={result['occlusion_activated']}"
        )
        if not result["occlusion_activated"]:
            print(f"  WARNING: {name} 剔除未激活（culled_ratio=0），"
                  f"speedup 数字是噪声，不构成剔除证据（BENCH-0015）")

    activated_groups = [r["group"] for r in results
                        if r.get("occlusion_activated")]
    warning_no_active_cull_group = None
    if results and not activated_groups:
        warning_no_active_cull_group = (
            "本次运行没有任何组激活剔除（全部 culled_ratio=0）——整份结果"
            "不构成遮挡剔除证据（历史 2K 版只有 B0 单轨组即此情形；"
            "BENCH-0015）。至少加入 B1/B3 全遮挡组复测。")
        print(f"WARNING: {warning_no_active_cull_group}")

    suffix = "" if args.material == "2k" else f"_{args.material}"
    out_path = Path(args.output) if args.output else (
        REPO / "benchmarks" / f"timeline_occlusion_bench_result{suffix}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "material": {
            "lower": {"file": _MATERIAL["lower"], "sha1_12": lower_sha},
            "upper": {"file": _MATERIAL["upper"], "sha1_12": upper_sha},
        },
        "params": {
            "warmup": args.warmup, "measure": args.measure, "runs": args.runs,
        },
        # BENCH-0015：CI 方法、剔除生效组与告警随 payload 落盘。
        "bootstrap": {
            "speedup_statistic": "median(off_s)/median(on_s)",
            "method": "paired percentile bootstrap over interleaved runs",
            "runs": args.runs,
            "stable": args.runs >= 3,
        },
        "runs_warning": runs_warning,
        "occlusion_activated_groups": activated_groups,
        "warning_no_active_cull_group": warning_no_active_cull_group,
        "results": results,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"results -> {out_path}")
    if args.require_cull and warning_no_active_cull_group:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
