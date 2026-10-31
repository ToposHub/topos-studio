#!/usr/bin/env python3
"""plan-01 驱动脚本：检查批次报告完成度，输出下一波任务，可自动更新 PROGRESS.md 并 git commit。

用法：
  python3 tools/check_progress.py            # 只打印状态与下一波 8 个任务
  python3 tools/check_progress.py --commit   # 更新 PROGRESS.md 摘要并 git commit（消息自动生成）
"""
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]  # docs/audit/2026-09-28
PROJ = BASE.parents[2]  # 仓库根 topos-color01

# 解析 INVENTORY 全部批次
batches = {}
for m in re.finditer(r'<details><summary>batch-(\d+) \[(P\d)\] <code>(.*?)</code>', (BASE / "INVENTORY.md").read_text()):
    bid, pri, topic = int(m.group(1)), m.group(2), m.group(3)
    batches[bid] = (pri, topic)

done = set()
for rp in (BASE / "batches").glob("batch-*.md"):
    m = re.match(r'batch-(\d+)', rp.stem)
    if m and int(m.group(1)) in batches:
        done.add(int(m.group(1)))
done.add(1)  # 空 __init__ 跳过

# WAVEPLAN 顺序
plan = [l.split(". ", 1)[1].strip() for l in (BASE / "assignments" / "WAVEPLAN.md").read_text().splitlines() if l.strip()]
assign_batches = {}  # assign_name -> [bid]
for a in plan:
    stub = (BASE / "assignments" / f"{a}.md").read_text()
    ids = [int(x) for x in re.findall(r'## 批次 batch-(\d+)', stub)]
    assign_batches[a] = ids

pending = [a for a in plan if not all(b in done for b in assign_batches[a])]
finished = [a for a in plan if a not in pending]

total = len(batches) - 1
print(f"batch done: {len(done)}/{total} | assignments finished: {len(finished)}/{len(plan)} | pending: {len(pending)}")
nxt = pending[:8]
if nxt:
    print("next wave:", ", ".join(nxt))

if "--commit" in sys.argv:
    # 统计发现数
    c = m_ = s = 0
    for rp in sorted((BASE / "batches").glob("batch-*.md")):
        for sev, key in (("Critical", c), ("Major", m_), ("Suggestion", s)):
            pass
    counts = {"Critical": 0, "Major": 0, "Suggestion": 0}
    for rp in (BASE / "batches").glob("batch-*.md"):
        t = rp.read_text(encoding="utf-8", errors="replace")
        for sev in counts:
            counts[sev] += len(re.findall(rf'^- \[AUD-\d+\] \[{sev}\]', t, re.M))
    # 更新 PROGRESS.md
    p = BASE / "PROGRESS.md"
    t = p.read_text(encoding="utf-8")
    new_status = (f"批次完成 **{len(done)}/{total}**（P0 全部 + P3/P4 推进中）；"
                  f"累计发现 Critical {counts['Critical']} / Major {counts['Major']} / Suggestion {counts['Suggestion']}（含少量重复计入批次汇总行，以 ISSUES.md 台账为准）。")
    t = re.sub(r'(\| P3 审查 P1 优先级 \|[^|]*\|)[^|]*(\|)',
               rf'\1 🔄 推进中：见批次目录与 WAVEPLAN；{len(done)} 批已出报告 \2', t, count=1)
    if "## 滚动进度（脚本自动维护）" not in t:
        t += "\n## 滚动进度（脚本自动维护）\n\n" + new_status + "\n"
    else:
        t = re.sub(r'## 滚动进度（脚本自动维护）\n\n.*$', '## 滚动进度（脚本自动维护）\n\n' + new_status + '\n', t, flags=re.S)
    p.write_text(t, encoding="utf-8")
    msg = f"[plan-01] progress: {len(done)}/{total} batches audited (C{counts['Critical']}/M{counts['Major']}/S{counts['Suggestion']})"
    subprocess.run(["git", "add", str(BASE.relative_to(PROJ))], cwd=PROJ, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=PROJ, check=True)
    print("committed:", msg)
