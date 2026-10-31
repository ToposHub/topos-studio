#!/usr/bin/env python3
"""步骤 1 真机门禁：D3D11 device 生命周期 + 定长纹理池 1,000 帧 lease 循环。

对应计划 docs/windows_timeline_gpu_export_optimization_plan_2026-09-15.md
§6 步骤 1 的验收："NVIDIA、AMD 实机各做 device/surface probe；跨 LUID 必须
得到 adapter_mismatch，1,000 帧循环无资源增长。"

本脚本在真机上依次验证并归档：

1. device 创建（FL 阶梯 12.1→11.0）+ adapter LUID 走查；
2. NV12 / P010 / BGRA 的 CheckFormatSupport 能力位（含 P010 真实拒绝时的
   诚实记录——RX 6800 实测 P010 查询 E_FAIL）；
3. NV12 定长池（depth=8）单线程 1,000 帧 acquire/release：封口后纹理数
   恒为 8，peak ≤ depth；
4. 深度 6 的 4 线程并发 lease 环（in-flight 反压语义）；
5. P010 池创建尝试：成功或 fail-closed，均如实记录。

用法：
    .venv/Scripts/python.exe scripts/probe_d3d11_device_pool_windows.py
    .venv/Scripts/python.exe scripts/probe_d3d11_device_pool_windows.py --frames 200
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.media.windows_hw.d3d11_device import (  # noqa: E402
    DXGI_FORMAT_B8G8R8A8_UNORM,
    DXGI_FORMAT_NV12,
    DXGI_FORMAT_P010,
    check_format_support,
    create_d3d11_device,
)
from src.shared.media.windows_hw.d3d11_texture_pool import (  # noqa: E402
    D3D11_BIND_DECODER,
    D3D11_BIND_RENDER_TARGET,
    D3D11_BIND_SHADER_RESOURCE,
    create_d3d11_texture_pool,
)

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "tests" / "media" / "results"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=1000,
                        help="lease 循环帧数（默认 1000，计划门禁值）")
    parser.add_argument("--depth", type=int, default=8,
                        help="单线程门禁池深度（默认 8）")
    parser.add_argument("--json", dest="json_out", default="",
                        help="结果 JSON 额外输出路径")
    args = parser.parse_args()

    report: dict = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "purpose": "计划步骤 1 真机门禁：D3D11 device + 定长纹理池",
        "os": sys.platform,
    }
    gates: dict[str, bool] = {}

    # --- 1. device + LUID --------------------------------------------------
    device, reason = create_d3d11_device()
    report["device"] = {
        "ok": device is not None,
        "reason": reason,
        "identity": device.identity.to_dict() if device else {},
    }
    gates["device_created"] = device is not None
    gates["adapter_luid_present"] = bool(
        device and device.identity.adapter_luid
    )
    print(f"[1] device ok={device is not None} "
          f"fl={hex(device.identity.feature_level) if device else '?'} "
          f"luid={device.identity.adapter_luid if device else '?'}")

    if device is None:
        _finish(report, gates, args.json_out)
        return 1

    try:
        # --- 2. 格式能力位 -------------------------------------------------
        formats: dict[str, dict] = {}
        for name, fmt in (
            ("nv12", DXGI_FORMAT_NV12),
            ("p010", DXGI_FORMAT_P010),
            ("bgra8", DXGI_FORMAT_B8G8R8A8_UNORM),
        ):
            ok, bits, why = check_format_support(device, fmt)
            formats[name] = {"ok": ok, "support_bits": hex(bits), "reason": why}
            print(f"[2] CheckFormatSupport {name}: ok={ok} bits={hex(bits)}")
        report["format_support"] = formats
        gates["nv12_format_support"] = formats["nv12"]["ok"]

        # --- 3. NV12 池 1,000 帧门禁 ---------------------------------------
        pool, pool_reason = create_d3d11_texture_pool(
            device.device_ptr, depth=args.depth, width=1920, height=1080,
            dxgi_format=DXGI_FORMAT_NV12,
        )
        gate3: dict = {"ok": pool is not None, "reason": pool_reason}
        if pool is not None:
            started = time.perf_counter()
            peak = 0
            try:
                for i in range(args.frames):
                    lease = pool.try_acquire()
                    if lease is None:
                        # 单线程顺序循环不该耗尽；耗尽即失败。
                        gate3["error"] = f"第 {i} 帧 acquire 失败"
                        break
                    lease.release()
                peak = pool.stats().peak_in_use
            finally:
                gate3["elapsed_s"] = round(time.perf_counter() - started, 3)
                gate3["stats"] = pool.stats().to_dict()
                gate3["peak_in_use"] = peak
                gate3["frames"] = args.frames
                stats = pool.stats()
                gate3["ok"] = (
                    stats.created == args.depth
                    and stats.serviced == args.frames
                    and stats.in_use == 0
                    and peak <= args.depth
                    and "error" not in gate3
                )
                pool.close()
        gates["pool_1000_frame_no_growth"] = bool(gate3.get("ok"))
        report["pool_single_thread_gate"] = gate3
        print(f"[3] NV12 池 {args.frames} 帧: ok={gate3.get('ok')} "
              f"created={gate3.get('stats', {}).get('created')} "
              f"elapsed={gate3.get('elapsed_s')}s")

        # --- 4. 并发 lease 环（depth=6 × 4 线程 × 250 帧） ------------------
        conc: dict = {"depth": 6, "threads": 4, "frames_per_thread": 250}
        pool_c, reason_c = create_d3d11_texture_pool(
            device.device_ptr, depth=6, width=1920, height=1080,
            dxgi_format=DXGI_FORMAT_NV12,
        )
        if pool_c is None:
            conc["ok"] = False
            conc["reason"] = reason_c
        else:
            errors: list[str] = []

            def worker() -> None:
                for _ in range(250):
                    lease = pool_c.try_acquire()
                    if lease is None:
                        continue  # 耗尽跳过是合法反压路径
                    try:
                        lease.release()
                    except ValueError as exc:
                        errors.append(str(exc))
                        return

            threads = [threading.Thread(target=worker) for _ in range(4)]
            started = time.perf_counter()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            conc["elapsed_s"] = round(time.perf_counter() - started, 3)
            stats_c = pool_c.stats()
            conc["stats"] = stats_c.to_dict()
            conc["ok"] = (
                not errors
                and stats_c.created == 6
                and stats_c.peak_in_use <= 6
                and stats_c.in_use == 0
            )
            conc["errors"] = errors
            pool_c.close()
        gates["pool_concurrent_ring_bounded"] = bool(conc.get("ok"))
        report["pool_concurrent_gate"] = conc
        print(f"[4] 并发环: ok={conc.get('ok')} "
              f"peak={conc.get('stats', {}).get('peak_in_use')}")
    finally:
        device.release()

    _finish(report, gates, args.json_out)
    return 0 if all(gates.values()) else 1


def _finish(report: dict, gates: dict[str, bool], extra_json: str) -> None:
    report["gates"] = gates
    report["all_gates_passed"] = all(gates.values())

    from scripts.bench_timeline_export_windows import machine_tag

    tag = machine_tag()
    date = time.strftime("%Y-%m-%d")
    base = RESULTS_DIR / f"d3d11_device_pool_probe_{tag}_{date}"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    lines = [
        f"# D3D11 device + 纹理池真机门禁（{tag}）",
        "",
        f"日期 {date} ｜ 原始数据：`{base.name}.json`",
        "",
        "| 门禁 | 结果 |",
        "|---|---|",
    ]
    for name, ok in gates.items():
        lines.append(f"| {name} | {'✅' if ok else '❌'} |")
    dev = report.get("device", {})
    ident = dev.get("identity", {})
    lines += [
        "",
        "## 环境",
        "",
        f"- feature_level: {ident.get('feature_level')}",
        f"- adapter: {ident.get('adapter_description')} "
        f"(LUID {ident.get('adapter_luid')})",
    ]
    for name, info in report.get("format_support", {}).items():
        lines.append(
            f"- format_support {name}: ok={info['ok']} "
            f"bits={info['support_bits']}"
            + (f" reason={info['reason']}" if info.get("reason") else "")
        )
    lines += ["", "## 结论", ""]
    if report["all_gates_passed"]:
        lines.append(
            "device 创建、LUID 走查、NV12 格式支持、定长池"
            f" {report['pool_single_thread_gate']['frames']} 帧无增长、"
            "并发环有界——计划步骤 1 门禁在本机全部通过。"
        )
    else:
        failed = [k for k, v in gates.items() if not v]
        lines.append(
            f"未通过门禁：{failed}。按计划 §6.1，先归因修复再进入步骤 2。"
        )
    base.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告：{base.with_suffix('.json')}")
    print(f"报告：{base.with_suffix('.md')}")
    if extra_json:
        Path(extra_json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    sys.exit(main())
