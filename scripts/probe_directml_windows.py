#!/usr/bin/env python3
"""W4-03 前置 / 开放问题 6 决策证据：onnxruntime-directml 探针。

**在临时 venv 运行**（不进项目依赖）——依赖决策的证据收集，不是依赖引入。

内容：
1. 构建小型 Conv 栈 ONNX 模型（3×Conv+Relu，720p 分辨率，降噪类形状）；
2. CPU EP vs DML EP：输出一致性校验 + 冷启动（session 创建，含 DML
   shader 编译）+ 热稳态推理时延对照；
3. 记录包体占用与许可信息——依赖决策的三要素（性能/体积/许可）。

用法（临时 venv）：
    %TEMP%\\topos_dml_probe_venv\\Scripts\\python.exe scripts/probe_directml_windows.py
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "tests" / "media" / "results"

H, W, CH = 720, 1280, 16
MODEL_PATH = Path(tempfile.gettempdir()) / "dml_probe_model.onnx"


def build_model() -> None:
    import onnx
    from onnx import TensorProto, helper

    np.random.seed(2026)
    X = helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3, H, W])
    Y = helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 3, H, W])
    inits, nodes = [], []
    shapes = [(3, CH), (CH, CH), (CH, 3)]
    prev = "x"
    for i, (cin, cout) in enumerate(shapes, 1):
        w = helper.make_tensor(
            f"w{i}", TensorProto.FLOAT, [cout, cin, 3, 3],
            (np.random.randn(cout, cin, 3, 3) * 0.05).astype(np.float32).tobytes(),
            raw=True)
        inits.append(w)
        nodes.append(helper.make_node("Conv", [prev, f"w{i}"], [f"h{i}"],
                                      pads=[1, 1, 1, 1]))
        prev = f"h{i}"
        if i < len(shapes):
            nodes.append(helper.make_node("Relu", [prev], [f"r{i}"]))
            prev = f"r{i}"
    nodes.append(helper.make_node("Identity", [prev], ["y"]))
    graph = helper.make_graph(nodes, "dml_probe", [X], [Y], inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    onnx.checker.check_model(model)
    onnx.save(model, str(MODEL_PATH))


def package_footprint() -> dict:
    out = {}
    for dist_name in ("onnxruntime-directml", "onnx", "numpy"):
        try:
            dist = importlib.metadata.distribution(dist_name)
            # dist-info 目录的父级即 site-packages（locate_file("") 不可靠）
            site = dist._path.parent
            total = 0
            seen: set[str] = set()
            stem = dist_name.replace("-", "_")
            for d in site.glob(f"{stem}*"):
                if d.is_dir() and d.name not in seen:
                    seen.add(d.name)
                    total += sum(f.stat().st_size for f in d.rglob("*")
                                 if f.is_file())
            # onnxruntime-directml 的包目录名为 onnxruntime
            if dist_name == "onnxruntime-directml" and (site / "onnxruntime").is_dir():
                d = site / "onnxruntime"
                total += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            meta = dist.metadata
            out[dist_name] = {
                "version": dist.version,
                "size_mb": round(total / (1024 * 1024), 1),
                "license": (meta.get("License") or
                            (meta.get("License-Expression") or "?"))[:60],
            }
        except Exception as exc:
            out[dist_name] = f"unavailable: {exc}"
    return out


def bench(providers: list[str], x: np.ndarray, warmup: int, iters: int) -> dict:
    import onnxruntime as ort

    t0 = time.perf_counter()
    sess_opts = ort.SessionOptions()
    sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess = ort.InferenceSession(MODEL_PATH, sess_opts, providers=providers)
    create_s = time.perf_counter() - t0
    actual = sess.get_providers()

    for _ in range(warmup):
        sess.run(None, {"x": x})
    times = []
    for _ in range(iters):
        t = time.perf_counter()
        sess.run(None, {"x": x})
        times.append((time.perf_counter() - t) * 1000)
    times_ms = sorted(times)
    return {
        "requested_providers": providers,
        "active_providers": actual,
        "session_create_s": round(create_s, 2),
        "iters": iters,
        "mean_ms": round(sum(times) / len(times), 2),
        "p50_ms": round(times_ms[len(times_ms) // 2], 2),
        "min_ms": round(times_ms[0], 2),
        "_sess": sess,
    }


def main() -> int:
    import onnxruntime as ort

    build_model()
    np.random.seed(7)
    x = np.random.rand(1, 3, H, W).astype(np.float32) * 0.5

    cpu = bench(["CPUExecutionProvider"], x, warmup=3, iters=10)
    dml = bench(["DmlExecutionProvider"], x, warmup=5, iters=30)
    # 一致性：DML 输出 vs CPU 输出
    y_cpu = cpu.pop("_sess").run(None, {"x": x})[0]
    y_dml = dml.pop("_sess").run(None, {"x": x})[0]
    diff = float(np.abs(y_cpu - y_dml).max())
    denom = max(float(np.abs(y_cpu).max()), 1e-6)

    report = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "plan_item": "W4-03 前置 / 开放问题 6：onnxruntime-directml 决策证据",
        "environment": {"python": sys.version.split()[0],
                        "ort_version": ort.__version__,
                        "available_providers": ort.get_available_providers()},
        "model": {"shape": f"1x3x{H}x{W}", "topology": "Conv+Relu x2 + Conv"},
        "cpu_ep": cpu,
        "dml_ep": dml,
        "output_consistency": {
            "max_abs_diff": round(diff, 5),
            "relative_to_peak": round(diff / denom, 5),
            "note": "DML 为 GPU float 路径，1e-2 级相对差属正常",
        },
        "package_footprint": package_footprint(),
        "dml_available": "DmlExecutionProvider" in dml["active_providers"],
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / "directml_probe_amd_rx6800.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("cpu_ep", "dml_ep")}, ensure_ascii=False, indent=2))
    print(f"CPU EP: {cpu['mean_ms']} ms/iter (create {cpu['session_create_s']}s)")
    print(f"DML EP: {dml['mean_ms']} ms/iter (create {dml['session_create_s']}s, "
          f"active={dml['active_providers']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
