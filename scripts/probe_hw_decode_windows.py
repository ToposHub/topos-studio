"""Windows 硬解后端探测：用真实素材逐个验证 PyAV HWAccel 设备类型是否可用。

用法: .venv/Scripts/python.exe scripts/probe_hw_decode_windows.py <video_path>
输出: 每个设备类型能否解码首帧、输出像素格式、单帧耗时，并保存 JSON/MD。
"""
from __future__ import annotations

import json
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import av
from av.codec.hwaccel import HWAccel, HWDeviceType


def probe(path: str) -> dict:
    container_info = av.open(path)
    stream = container_info.streams.video[0]
    codec_name = stream.codec_context.name
    pix_fmt = stream.codec_context.pix_fmt
    width, height = stream.codec_context.width, stream.codec_context.height
    container_info.close()
    source = {
        "path": str(Path(path)),
        "codec": codec_name,
        "pix_fmt": pix_fmt,
        "width": int(width),
        "height": int(height),
    }
    print(f"素材: {path}")
    print(f"  codec={codec_name} pix_fmt={pix_fmt} size={width}x{height}")

    rows = []
    for member in HWDeviceType:
        t0 = time.perf_counter()
        try:
            accel = HWAccel(member)
            c = av.open(path, hwaccel=accel)
            s = c.streams.video[0]
            n = 0
            first_fmt = None
            for frame in c.decode(s):
                if first_fmt is None:
                    first_fmt = frame.format.name
                n += 1
                if n >= 5:
                    break
            dt = (time.perf_counter() - t0) * 1000
            row = {
                "device": member.name.lower(), "status": "ok", "frames": n,
                "output_format": first_fmt, "elapsed_ms": round(dt, 1),
            }
            rows.append(row)
            print(f"  [{member.name:12s}] OK   frames={n} out_fmt={first_fmt} open+5帧={dt:.0f}ms")
            c.close()
        except Exception as exc:  # noqa: BLE001
            dt = (time.perf_counter() - t0) * 1000
            msg = str(exc).split("\n")[0][:90]
            rows.append({
                "device": member.name.lower(), "status": "failed", "frames": 0,
                "output_format": None, "elapsed_ms": round(dt, 1),
                "error": f"{type(exc).__name__}: {msg}",
            })
            print(f"  [{member.name:12s}] FAIL {dt:5.0f}ms  {msg}")
    return {
        "date": datetime.now().isoformat(timespec="seconds"),
        "plan_item": "W2 hardware decoder capability probe (download path only)",
        "os": sys.platform,
        "source": source,
        "protocol": "真实 PyAV open + 首 5 帧 decode；输出格式与耗时记录；不等于 D3D11 zero-copy",
        "rows": rows,
    }


def _write_report(report: dict, tag_override: str = "") -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    results_dir = root / "tests" / "media" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    try:
        from scripts.bench_timeline_export_windows import machine_tag

        tag = tag_override or machine_tag()
    except Exception:
        tag = tag_override or "unknown"
    source_stem = Path(str(report["source"]["path"])).stem
    safe_stem = "".join(ch.lower() if ch.isalnum() else "_" for ch in source_stem)
    date = str(report["date"])[:10]
    base = results_dir / f"windows_hw_decode_probe_{tag}_{safe_stem}_{date}"
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_lines = [
        f"# Windows 硬解能力探针（{tag}）", "",
        f"日期：{report['date']} ｜ 输入：`{report['source']['path']}`", "",
        f"协议：{report['protocol']}", "",
        "| 设备类型 | 状态 | 帧数 | 输出格式 | 耗时 ms | 错误 |",
        "|---|---|---:|---|---:|---|",
    ]
    for row in report["rows"]:
        md_lines.append(
            f"| {row['device']} | {row['status']} | {row['frames']} | "
            f"{row.get('output_format') or '—'} | {row['elapsed_ms']} | "
            f"{row.get('error', '')} |"
        )
    md_lines += [
        "", "## 边界", "",
        "此探针只证明 PyAV 后端可以打开并解码样本帧；硬解输出被下载/转换到当前 Python 路径时，不能作为 D3D11 全链路 GPU 证据。",
        "",
    ]
    md_path = base.with_suffix(".md")
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    return json_path, md_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_path")
    parser.add_argument("--tag", default="", help="机器 slug；默认自动探测")
    args = parser.parse_args()
    result = probe(args.video_path)
    json_path, md_path = _write_report(result, args.tag)
    print(f"\nwritten: {json_path}\nwritten: {md_path}")
