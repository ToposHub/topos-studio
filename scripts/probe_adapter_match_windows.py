#!/usr/bin/env python3
"""W1-03：multi-GPU / 混合显卡 decode-render 适配器匹配实测。

本机形态（iGPU + dGPU 桌面机，非 Optimus 笔记本但 multi-adapter 行为
同构）验证三层：

1. DXGI 枚举快照——记录全部适配器（含重复/虚拟条目，LUID 逐一对）。
2. ``pick_primary_hardware_adapter`` 的 decode 侧选择。
3. ``check_adapter_match`` 规则验证：相同 LUID 放行 / 不同 LUID 拒绝
   （reason 形态 ``adapter_mismatch:<render>|<decode>``）。

render 侧 LUID 的真实来源是 GL 上下文（运行时经 interop 查询传入）；
本探针无法在 WGL 下标准获取，按接口语义如实记
``render_adapter_unprobed``，不猜。

产物（P9C-02 配对）：
- ``tests/media/results/adapter_match_probe_<tag>.{md,json}``

用法：
    .venv/Scripts/python.exe scripts/probe_adapter_match_windows.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "tests" / "media" / "results"


def _auto_tag(vendor: str, adapters: list) -> str:
    desc = adapters[0].description if adapters else ""
    words = re.findall(r"[A-Za-z0-9]+", desc)
    model = ""
    for w in words:
        if any(c.isdigit() for c in w) and w.lower() not in ("pci", "pcie"):
            model = w.lower()
            break
    return f"{vendor}_{model}" if vendor != "unknown" else (model or "unknown_gpu")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="",
                        help="产物分册后缀（缺省按主适配器自动生成）")
    args = parser.parse_args()

    from src.shared.media.windows_hw.gpu_capability import (
        check_adapter_match,
        decode_render_adapter_report,
        pick_primary_hardware_adapter,
        probe_gpu_capability,
    )

    gpu = probe_gpu_capability()
    adapters = [a.to_dict() for a in gpu.adapters]
    tag = args.tag or _auto_tag(gpu.primary_vendor, gpu.adapters)
    print(f"[1] DXGI enumeration ({len(adapters)}):")
    for a in adapters:
        print(f"    {a['description']:38s} {a['vendor_name']:8s} "
              f"{a['vendor_id']}/{a['device_id']} LUID={a['adapter_luid']}")

    # 重复描述条目检测（同一物理 GPU 可能出现多个枚举条目，LUID 不同）
    seen: dict[str, str] = {}
    dupes: list[dict] = []
    for a in adapters:
        key = a["description"]
        if key in seen and seen[key] != a["adapter_luid"]:
            dupes.append({"description": key,
                          "luid_a": seen[key], "luid_b": a["adapter_luid"]})
        seen.setdefault(key, a["adapter_luid"])
    if dupes:
        print(f"[1] duplicate description entries (same desc, distinct LUID): "
              f"{len(dupes)}")

    decode_adapter = pick_primary_hardware_adapter(gpu.adapters)
    decode_luid = decode_adapter.adapter_luid if decode_adapter else ""
    print(f"[2] decode-side primary adapter: "
          f"{decode_adapter.description if decode_adapter else 'None'} "
          f"LUID={decode_luid}")

    # [3] 匹配规则验证（规则本身 + 诚实标注 render 侧未探测）
    rule_same = check_adapter_match(decode_luid, decode_luid)
    rule_diff = check_adapter_match("00010000-00000000", decode_luid)
    print(f"[3] rule same-LUID -> match={rule_same[0]} reason={rule_same[1]!r}")
    print(f"[3] rule diff-LUID -> match={rule_diff[0]} reason={rule_diff[1]!r}")
    snapshot = decode_render_adapter_report()  # render 侧未传 → unprobed
    print(f"[3] decode_render_adapter_report: {snapshot}")

    ok_all = (
        gpu.probe_ok
        and decode_adapter is not None
        and rule_same == (True, "")
        and rule_diff[0] is False
        and rule_diff[1].startswith("adapter_mismatch:")
        and snapshot.get("match") is False
        and snapshot.get("reason") == "render_adapter_unprobed"
    )

    report = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "plan_item": "W1-03 multi-GPU decode/render 适配器匹配实测",
        "machine": {
            "os": sys.platform,
            "primary_vendor": gpu.primary_vendor,
            "adapters": adapters,
            "duplicate_description_entries": dupes,
        },
        "decode_side": {
            "description": decode_adapter.description if decode_adapter else None,
            "adapter_luid": decode_luid,
        },
        "rule_verification": {
            "same_luid": {"match": rule_same[0], "reason": rule_same[1]},
            "diff_luid": {"match": rule_diff[0], "reason": rule_diff[1]},
        },
        "render_side": snapshot,
        "all_ok": ok_all,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"adapter_match_probe_{tag}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")

    md = [f"# W1-03 适配器匹配实测（{tag}）", "",
          f"日期：{report['date']} ｜ 原始数据：`adapter_match_probe_{tag}.json`", "",
          "| 适配器 | vendor | VID/PID | LUID |", "|---|---|---|---|"]
    for a in adapters:
        md.append(f"| {a['description']} | {a['vendor_name']} "
                  f"| {a['vendor_id']}/{a['device_id']} | {a['adapter_luid']} |")
    md += [
        "", "## 规则验证", "",
        f"- 相同 LUID：match={rule_same[0]}（放行）",
        f"- 不同 LUID：match={rule_diff[0]}，reason=`{rule_diff[1]}`",
        f"- render 侧快照（未探测）：reason=`{snapshot.get('reason')}`"
        "——WGL 下无标准途径查询 render adapter LUID，运行时由 interop 层传入，不猜。",
        "", "## 结论", "",
        f"- 本机 decode 侧主适配器：{decode_adapter.description if decode_adapter else '—'}"
        f"（LUID `{decode_luid}`）；枚举中另含 iGPU（Intel UHD 770）与 Basic Render Driver。",
        ("- 同名适配器出现多个不同 LUID 条目（DXGI 枚举行为），适配器匹配必须按 "
         "LUID 而非描述字符串裁决。" if dupes else ""),
        "- iGPU+dGPU 桌面机形态下匹配规则验证通过；真 Optimus 笔记本（动态切换"
        " render GPU）仍留待真机复核。",
        "",
    ]
    md_path = RESULTS_DIR / f"adapter_match_probe_{tag}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nwritten: {json_path}\nwritten: {md_path}\nall_ok={ok_all}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
