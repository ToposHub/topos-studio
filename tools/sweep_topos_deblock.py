#!/usr/bin/env python3
"""去块滤波参数扫描：同一码流换滤波参数（滤波为纯解码端，无需重编码）。

用 `TC_DB_TUNE=1` 构建的库（``native/topos_codec/build/tune/``）经环境变量
覆盖 deblock.c 的旋钮，一次进程一个设置，输出块效应 / PSNR / 锐度 / 解码
吞吐——用于在"块界台阶↓"与"真实边缘/纹理不受伤"之间找工作点。

用法::

    .venv/bin/python tools/sweep_topos_deblock.py \
        --file bench_out/qgain/base_2K_proxy.mov \
        --set "v1:DB_W0=4,DB_W1=1" --set "v2:DB_W0=12,DB_W1=-4"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TUNE_LIB = REPO / "native" / "topos_codec" / "build" / "tune" / "topos_codec.dylib"
INNER = REPO / "tools" / "_db_sweep_inner.py"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="已有 TOPOS 码流（.mov）")
    ap.add_argument("--set", action="append", default=[],
                    help="名字:ENV1=V1,ENV2=V2")
    ap.add_argument("--snap", type=Path,
                    default=REPO / "bench_out" / "qgain" / "sweep_snap.json",
                    help="把胜出设置的滤波输出写到这里供可视化")
    args = ap.parse_args()
    if not TUNE_LIB.is_file():
        raise SystemExit(f"缺调参库：{TUNE_LIB}（先构建 build/tune）")

    results = []
    for spec in args.set:
        name, _, rest = spec.partition(":")
        env = dict(os.environ)
        env["TOPOS_CODEC_LIB"] = str(TUNE_LIB)
        parsed = {}
        if rest:
            for kv in rest.split(","):
                k, _, v = kv.partition("=")
                env[k] = v
                parsed[k] = v
        out = subprocess.run(
            [sys.executable, str(INNER), "--file", str(args.file),
             "--name", name, "--snap", str(args.snap)],
            env=env, capture_output=True, text=True, cwd=str(REPO))
        tail = (out.stderr or "").strip().splitlines()
        tail = [t for t in tail if "deblock tune" in t]
        if tail:
            print(f"  {tail[-1]}", flush=True)
        if out.returncode != 0:
            print(f"[{name}] 失败：{out.stderr[-800:]}", flush=True)
            continue
        row = json.loads(out.stdout.strip().splitlines()[-1])
        row["name"] = name
        row["env"] = parsed
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    keys = ("block_h", "block_v", "y_psnr", "y_psnr_edge", "sharp", "grid",
            "stair", "dec_fps")
    print("\n" + f"{'设置':<14}" + "".join(f"{k:>13}" for k in keys))
    for r in results:
        print(f"{r['name']:<14}"
              + "".join(f"{r[k]:>13.4g}" if isinstance(r[k], float)
                        else f"{r[k]:>13}" for k in keys))
    out_json = REPO / "bench_out" / "qgain" / "deblock_sweep.json"
    out_json.write_text(json.dumps(
        {"file": args.file, "results": results}, indent=1, ensure_ascii=False))
    print(f"\nreport → {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
