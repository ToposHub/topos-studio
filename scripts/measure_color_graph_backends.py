#!/usr/bin/env python
"""B12.4 三后端能力矩阵实测脚本 —— F01/F02/F03 fixture 的 CPU/GL/Metal 误差。

产出 ``docs/validation/color_nodes/backend_matrix.md``：机器/驱动/颜色
上下文、逐 fixture×参数域的后端误差与门禁判定。**诚实规则**（计划
B12）：缺硬件/缺后端标 NOT RUN，不记 PASS；能力声明与实测不符的项
降级为未就绪。

用法：
    QT_QPA_PLATFORM=offscreen .venv/bin/python \
        scripts/measure_color_graph_backends.py \
        [--out docs/validation/color_nodes/backend_matrix.md] [--json OUT]

对照参考为 CPU 生产 oracle（N09）；CPU↔GL parity 由
tests/color/test_color_graph_cpu_parity.py 的同门禁独立锁定。
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.features.color.core.color_graph_compiler import compile_graph  # noqa: E402
from src.features.color.core.color_graph_cpu import (  # noqa: E402
    corrector_production_oracle,
    run_plan_cpu,
)
from src.features.color.core.color_graph_metal import (  # noqa: E402
    ColorGraphMetalExecutor,
)
from src.features.color.core.color_graph_migration import (  # noqa: E402
    migrate_legacy_stack,
)
from src.features.color.core.color_graph_snapshot import freeze_graph  # noqa: E402
from src.shared.gpu.metal.context import MetalComputeContext  # noqa: E402
from src.shared.project.color_graph import unfreeze_params  # noqa: E402
from tests.color.fixtures.color_graph import (  # noqa: E402
    f01_hdr_ramp,
    f02_patches,
    _neutral_layer,
)
from tests.color.test_color_graph_migration import _stack_with_layers  # noqa: E402

SDR_TOL = 1.0 / 255.0
HDR_REL = 2e-3


def _layers_rich():
    layer = _neutral_layer("Rich")
    layer.grading_params.update({
        "exposure": 25.0, "temp": 12.0, "tint": -6.0, "contrast": 30.0,
        "saturation": -18.0, "vibrance": 25.0, "shadow": 12.0,
        "highlights": -20.0, "hue_shift": 40.0,
        "lift_saturation": 10.0, "gamma_saturation": -10.0,
        "gain_saturation": 5.0,
        "lift": (20.0, 0.3, 0.05), "gamma": (200.0, 0.2, -0.04),
        "gain": (120.0, 0.15, 0.08),
    })
    return layer


def _layers_curves():
    from src.features.color.core.layer import build_curves_lut_from_data
    layer = _neutral_layer("Curve")
    layer.curves_data = {"M": [(0, 255), (64, 209), (191, 46), (255, 0)]}
    layer.curves_lut = build_curves_lut_from_data(layer.curves_data)
    layer.grading_params["exposure"] = 8.0
    return layer


CASES = [
    ("F01 neutral", lambda: [_neutral_layer("N")], lambda: f01_hdr_ramp(64)),
    ("F01 rich-basic", lambda: [_layers_rich()], lambda: f01_hdr_ramp(64)),
    ("F01 curves", lambda: [_layers_curves()], lambda: f01_hdr_ramp(64)),
    ("F01 serial A→B", lambda: [_layers_rich(), _layers_curves()],
     lambda: f01_hdr_ramp(64)),
    ("F02 rich-basic", lambda: [_layers_rich()], lambda: f02_patches(64)),
    ("F02 curves", lambda: [_layers_curves()], lambda: f02_patches(64)),
]


def _provider(graph):
    from src.features.color.core.layer import build_curves_lut_from_data

    payloads = {n.node_id: unfreeze_params(dict(n.legacy_payload))
                for n in graph.nodes}

    def provide(node_id):
        payload = dict(payloads.get(node_id) or {})
        node = next((n for n in graph.nodes if n.node_id == node_id), None)
        if node is not None:
            payload["visible"] = node.enabled
        if payload.get("curves_data"):
            payload["curves_lut"] = build_curves_lut_from_data(
                dict(payload["curves_data"]))
        return payload
    return provide


def _metrics(candidate: np.ndarray, reference: np.ndarray) -> dict:
    diff = np.abs(candidate.astype(np.float64) - reference.astype(np.float64))
    mag = np.maximum(np.abs(reference.astype(np.float64)), 1e-6)
    sdr = np.abs(reference) <= 1.0
    sdr_abs = float(diff[sdr].max()) if sdr.any() else 0.0
    hdr_rel = float((diff[~sdr] / mag[~sdr]).max()) if (~sdr).any() else 0.0
    return {
        "sdr_abs": round(sdr_abs, 6),
        "hdr_rel": round(hdr_rel, 6),
        "pass": bool(sdr_abs <= SDR_TOL and hdr_rel <= HDR_REL),
    }


def _cpu_reference(layers, source):
    graph = migrate_legacy_stack(_stack_with_layers(layers))
    provider = _provider(graph)
    plan = compile_graph(freeze_graph(graph, session_id="measure"),
                         size=source.shape[:2][::-1])
    quantized = source.astype(np.float16).astype(np.float32)
    result = run_plan_cpu(plan, quantized, lambda p: provider(p.node_id))
    return graph, provider, plan, quantized, result.output


def _gl_output(plan, provider, source_f32, size):
    import moderngl
    ctx = moderngl.create_standalone_context(require=330)
    try:
        from src.features.color.core.color_graph_executor import (
            ColorGraphExecutor,
        )
        executor = ColorGraphExecutor(ctx)
        w, h = size
        src_tex = ctx.texture((w, h), components=4, dtype="f2",
                              data=source_f32.astype(np.float16))
        result = executor.execute_plan(plan, src_tex, request_id="measure",
                                       source_size=(w, h),
                                       layer_param_provider=provider)
        if not result.published:
            return None, result.diagnostic_message
        fbo = ctx.framebuffer(color_attachments=[result.output_texture])
        fbo.use()
        out = np.frombuffer(fbo.read(components=4, dtype="f2"),
                            dtype=np.float16).reshape(h, w, 4)
        out = out.astype(np.float32)
        executor.release_revision("measure")
        src_tex.release()
        return out, None
    finally:
        ctx.release()


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001 —— 只读诊断
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(
        REPO / "docs/validation/color_nodes/backend_matrix.md"))
    parser.add_argument("--json", default=None,
                        help="可选 JSON 输出路径（机器可读矩阵）")
    args = parser.parse_args()

    machine = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "git": _git_sha(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
    }

    # GL renderer
    gl_info = {"status": "NOT RUN", "renderer": None}
    try:
        import moderngl
        ctx = moderngl.create_standalone_context(require=330)
        gl_info = {"status": "OK",
                   "renderer": ctx.info.get("GL_RENDERER", "unknown")}
        ctx.release()
    except Exception as exc:  # noqa: BLE001
        gl_info = {"status": f"NOT RUN ({exc})"}

    metal_info = {"status": "NOT RUN", "device": None}
    if MetalComputeContext.is_supported():
        metal_info = {"status": "OK",
                      "device": MetalComputeContext().device_name}

    rows = []
    for name, make_layers, make_source in CASES:
        layers = make_layers()
        source = make_source()
        graph, provider, plan, quantized, cpu_ref = _cpu_reference(
            layers, source)
        row = {"case": name, "cpu": {"status": "OK", "sdr_abs": 0.0,
                                     "hdr_rel": 0.0, "pass": True}}

        # GL
        if gl_info["status"] == "OK":
            try:
                gl_out, err = _gl_output(plan, provider, quantized,
                                         source.shape[:2][::-1])
                row["gl"] = ({"status": f"FAILED: {err}"}
                             if err else {"status": "OK",
                                          **_metrics(gl_out, cpu_ref)})
            except Exception as exc:  # noqa: BLE001
                row["gl"] = {"status": f"FAILED: {exc}"}
        else:
            row["gl"] = {"status": "NOT RUN"}

        # Metal
        if metal_info["status"] == "OK":
            try:
                mctx = MetalComputeContext()
                executor = ColorGraphMetalExecutor(mctx)
                result = executor.execute_plan(
                    plan, quantized.astype(np.float16), request_id="m",
                    layer_param_provider=provider)
                row["metal"] = (
                    {"status": f"FAILED: {result.diagnostic_code}"}
                    if not result.published
                    else {"status": "OK",
                          **_metrics(result.output.astype(np.float32),
                                     cpu_ref)})
                executor.release_revision("m")
            except Exception as exc:  # noqa: BLE001
                row["metal"] = {"status": f"FAILED: {exc}"}
        else:
            row["metal"] = {"status": "NOT RUN"}
        rows.append(row)

    # ── Markdown 报告 ──
    lines = [
        "# 三后端能力矩阵实测（B12.4）", "",
        f"- 时间：{machine['timestamp']}；git `{machine['git']}`",
        f"- 平台：{machine['platform']}（{machine['machine']}），"
        f"Python {machine['python']}",
        f"- GL：{gl_info['status']}"
        + (f"（{gl_info['renderer']}）" if gl_info.get("renderer") else ""),
        f"- Metal：{metal_info['status']}"
        + (f"（{metal_info['device']}）" if metal_info.get("device") else ""),
        "- 颜色上下文：scene-linear HDR（RGBA16F 中转；F01 含 −0.25/16 "
        "锚点，F02 含透明边）；参考 = CPU 生产 oracle（N09）",
        f"- 门禁：SDR abs ≤ {SDR_TOL:.2e}；HDR rel ≤ {HDR_REL:.0e}"
        "（计划 §7.1 / G2）", "",
        "| fixture × 参数域 | CPU | GL | Metal |",
        "|---|---|---|---|",
    ]

    def _cell(entry: dict) -> str:
        if entry["status"] != "OK":
            return entry["status"]
        flag = "PASS" if entry["pass"] else "**FAIL**"
        return (f"{flag}（SDR {entry['sdr_abs']:.1e} / "
                f"HDR rel {entry['hdr_rel']:.1e}）")

    for row in rows:
        lines.append(f"| {row['case']} | {_cell(row['cpu'])} | "
                     f"{_cell(row['gl'])} | {_cell(row['metal'])} |")
    lines += [
        "", "## 未覆盖（NOT RUN）", "",
        "- 其他平台（Windows/Linux、Apple Silicon、Intel GPU）未实测，"
        "不记 PASS；跨平台 golden 属 B16.3。",
        "- Corrector 越域参数（插件/邻域/HSL 曲线/LUT3D/fx/mixer/"
        "warper/soft-clip 曲线/非 Normal 混合）三后端一致 fail-closed"
        "（tests/color/test_color_graph_backend_capabilities.py），"
        "不进入本表。",
        "- qualifier GPU keyer 的图内生成（B10 债务）未并入，key 边仍为"
        "白 matte。",
        "",
        "## 能力目录快照", "",
        "```",
    ]
    from src.features.shared.effects.effect_catalog import (
        graph_backend_capability_matrix,
    )
    matrix = graph_backend_capability_matrix()
    header = f"{'op':24s} {'gl':12s} {'cpu':12s} {'metal':12s}"
    lines += [header, "-" * len(header)]
    for op, backends in matrix.items():
        lines.append(f"{op:24s} {backends['gl']:12s} {backends['cpu']:12s} "
                     f"{backends['metal']:12s}")
    lines.append("```")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"written: {out_path}")

    if args.json:
        payload = {"machine": machine, "gl": gl_info, "metal": metal_info,
                   "gate": {"sdr_abs": SDR_TOL, "hdr_rel": HDR_REL},
                   "cases": rows, "matrix": matrix}
        Path(args.json).write_text(
            json.dumps(payload, indent=1, ensure_ascii=False),
            encoding="utf-8")
        print(f"written: {args.json}")

    failed = [r for r in rows if any(
        v.get("status") == "OK" and not v.get("pass", True)
        for v in (r["cpu"], r["gl"], r["metal"]))]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
