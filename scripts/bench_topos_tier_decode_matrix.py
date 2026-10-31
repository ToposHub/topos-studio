#!/usr/bin/env python3
"""bench_topos_tier_decode_matrix —— 分辨率 × 档位解码速率矩阵报告聚合。

输入：topos_tier_decode_matrix 的 CSV（逐迭代行）；
输出：markdown 报告（p50/p95 帧率 + 实际码率 + sized 搜索选定的 qp）。

用法：
    python3 scripts/bench_topos_tier_decode_matrix.py \
        --csv <matrix.csv> --output docs/codec/topos_tier_decode_matrix_<date>.md \
        [--threads-label "16"]
"""
from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path

RES_ORDER = ["1920x1080", "3840x2160", "5760x3240", "7680x4320"]
RES_LABEL = {
    "1920x1080": "2K（1080p）",
    "3840x2160": "4K UHD",
    "5760x3240": "6K",
    "7680x4320": "8K UHD",
}
TIER_ORDER = ["proxy", "lt", "standard", "hq"]
TIER_LABEL = {
    "proxy": "Topos 422 Proxy",
    "lt": "Topos 422 LT",
    "standard": "Topos 422",
    "hq": "Topos 422 HQ",
}
KIND_LABEL = {"gradient": "渐变类（平滑内容）", "grain": "颗粒类（噪声内容）"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--threads-label", default="16")
    ap.add_argument("--machine", default="Intel i9-9900K（8C16T）")
    args = ap.parse_args()

    rows = []
    with open(args.csv, encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
        for line in fh:
            line = line.strip()
            if line:
                rows.append(dict(zip(header, line.split(","))))
    if not rows:
        raise SystemExit("empty csv")

    # (kind,res,tier,threads) -> decode_ms 样本；元数据取首行
    samples = defaultdict(list)
    meta = {}
    for r in rows:
        key = (r["kind"], f'{r["w"]}x{r["h"]}', r["tier"], int(r["threads"]))
        samples[key].append(float(r["decode_ms"]))
        meta[(key[0], key[1], key[2])] = (float(r["mbps"]), int(r["qp_used"]), float(r["bpp"]))

    kinds = sorted({k[0] for k in samples}, key=["gradient", "grain"].index)
    threads_set = sorted({k[3] for k in samples})
    res_present = [r for r in RES_ORDER if any(k[1] == r for k in samples)]

    lines = []
    lines.append("# Topos 422 分辨率 × 档位解码速率矩阵")
    lines.append("")
    lines.append(f"**日期：** {date.today().isoformat()}　**机器：** {args.machine}"
                 f"　**多线程：** {args.threads_label} 线程池　**帧率口径：** p50（中位），"
                 "逐迭代整帧 `tc_frame_decode`（真实输出平面，含 53B 帧头）")
    lines.append("")
    lines.append("**档位语义：** 产品 target_bpp（`src/shared/codec/topos_profiles.py`）——"
                 "同一档位跨分辨率为**同目标 bpp**（码率随像素数 ×4/档位递增），不是同 qp；"
                 "编码走 `tc_frame_encode_sized` 逐帧 qp 搜索（**0..95，含 v1.5 扩展域**，"
                 "ADR-C031）。内容为 image_synth 冻结种子合成 10-bit 4:2:2（渐变/颗粒两类），"
                 "码率按 24fps 折算 Mbps。")
    lines.append("")

    for kind in kinds:
        lines.append(f"## {KIND_LABEL.get(kind, kind)}")
        lines.append("")
        lines.append("| 分辨率 | 档位 | 目标 bpp | 实测码率 | 选定 qp | "
                     "解码 p50 (1t) | 解码 p50 (多t) | p95 (多t) |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for res in res_present:
            for tier in TIER_ORDER:
                key1 = (kind, res, tier, 1)
                if key1 not in samples:
                    continue
                mbps, qp, bpp = meta[(kind, res, tier)]
                t1 = 1000.0 / statistics.median(samples[key1])
                parts = [RES_LABEL.get(res, res), TIER_LABEL.get(tier, tier),
                         f"{bpp:.3f}", f"{mbps:.1f} Mbps", f"qp{qp}", f"{t1:.0f} fps"]
                for th in threads_set:
                    if th == 1:
                        continue
                    keyn = (kind, res, tier, th)
                    if keyn in samples:
                        p50 = statistics.median(samples[keyn])
                        p95 = statistics.quantiles(samples[keyn], n=20)[18]
                        parts.append(f"{1000.0 / p50:.0f} fps")
                        parts.append(f"{1000.0 / p95:.0f} fps")
                while len(parts) < 8:
                    parts.append("—")
                lines.append("| " + " | ".join(parts[:8]) + " |")
        lines.append("")

    lines.append("## 读法")
    lines.append("")
    lines.append("1. **码率随分辨率同档位 ×4 递增**是档位定义（固定 bpp）的结果。"
                 "qp 由逐帧 sized 搜索选定：颗粒类 Proxy 在全部分辨率命中 **qp66**"
                 "（v1.5 扩展域，旧 qp≤63 内核在该目标上会超支，见 ADR-C031）；"
                 "渐变类合成素材的高频码量极低，各档位目标均高于其（近）无损尺寸，"
                 "故 qp 顶到低值——真实素材不会出现这种塌缩。")
    lines.append("2. 同档位跨分辨率的**帧率近似恒定**：4K 帧比特数是 2K 的 4 倍、"
                 "像素也是 4 倍 → 每秒解码像素数（吞吐）近似恒定，档位内码率与"
                 "解码负载同步缩放。")
    lines.append("3. 渐变类显著快于颗粒类（同档位约 2-3×）：平滑内容粗档位量化后"
                 "接近全零块（DC-only 快路 + 长游程），颗粒类残余 ±1 系数多、"
                 "熵解码与重建更重。")
    lines.append("4. 本表档位 = 产品 target_bpp 语义（跨分辨率同 bpp）；与 ProRes "
                 "实测码率逐点对齐的对比见 `topos_vs_prores_decode_2026-09-10.md`"
                 "（该表为同码率就近匹配，两张表口径不同，不可混读）。")
    lines.append("")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
