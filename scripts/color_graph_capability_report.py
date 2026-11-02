#!/usr/bin/env python
"""B16.1 能力汇总与旧 layers 写入口扫描。

产出 ``docs/validation/color_nodes/capability_inventory.md``：

1. **action 表**：B01 ACTION_CONTRACTS（40 action × category/command）；
2. **provider 表**：效果目录（EffectCatalog）注册的效果与后端声明；
3. **backend 表**：图算子三后端能力矩阵（B12.1 实测数据源）；
4. **旧 layers 写入口扫描**：全仓检索 ``engine.layers`` /
   ``active_layer_index`` / ``color_stacks`` 的**写**点，逐个分类为
   ``迁移`` / ``投影`` / ``工程IO/兼容`` / ``过渡 LayerPanel`` /
   ``待审计``——分类记账（生产切换是 B16.5 门，当前旧路径写点按用途
   记账）；"待审计"应为零。

用法：
    QT_QPA_PLATFORM=offscreen .venv/bin/python \
        scripts/color_graph_capability_report.py \
        [--out docs/validation/color_nodes/capability_inventory.md]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 扫描白名单（按 B00 调用图分类；生产源码 = src/ 下非测试文件）
SCAN_ROOT = REPO / "src"

#: 写点分类（B16.1 语义：分类记账而非清零——生产切换是 B16.5 门；
#: "待审计"应为零）
_LEGACY_WRITE_CLASSES = {
    "src/features/color/core/color_graph_migration.py": "迁移（图↔layers 投影）",
    "src/features/color/adapters/color_stack_hydrator.py": "投影（图→引擎 hydrate）",
    "src/features/color/core/image_engine.py": "引擎内部状态（layers 容器本体）",
    "src/features/color/core/the_decompiler.py": "兼容 API（legacy 序列化）",
    "src/features/color/core/async_renderer.py": "渲染快照收集（读路径聚合）",
    "src/features/edit/core/timeline_export.py": "导出（图投影→grade payload）",
    "src/features/edit/core/edit_project_io.py": "工程 IO（legacy 载荷读写）",
    "src/features/edit/core/project.py": "栈权威容器（update_color_stack 单入口）",
    "src/features/edit/core/serialization.py": "序列化（legacy 载荷）",
    "src/features/scripting/api.py": "兼容 API（旧 layers 脚本面）",
    "src/shared/ai_models/swinir.py": "无关同名属性（误报豁免）",
    "src/shared/project/save_router.py": "工程 IO（save 路由）",
    "src/shared/project/topos_project.py": "工程 IO（topos 工程）",
    "src/app/controllers/deliver_controller.py": "工程加载/交付（legacy 载荷→引擎）",
    "src/app/controllers/file_controller.py": "工程加载/另存（legacy 载荷→引擎）",
    "src/app/controllers/edit_controller.py": "编辑宿主（读聚合/容器整理）",
    "src/app/controllers/color_controller.py": "兼容读路径（写形态命中需逐条甄别）",
    "src/app/controllers/layer_controller.py": "过渡 LayerPanel（B16.1：仅过渡使用，退场记账）",
}

def _scan_legacy_writes() -> list[dict]:
    """只匹配**写**形态（赋值/变异方法）；索引读取不匹配。"""
    pattern = re.compile(
        r"(?:engine|self\.engine|self\._engine)\.layers\s*"
        r"(?:=(?!=)|\.append|\.insert|\.remove|\.pop|\.extend|\.clear)"
        r"|self\.layers\s*(?:=(?!=)|\.append|\.insert|\.remove|\.pop"
        r"|\.extend|\.clear)"
        r"|\.active_layer_index\s*=(?!=)"
        r"|(?:self\.)?color_stacks\s*(?:\[[^\]]+\]\s*=(?!=)|\.pop\()"
        r"|(?:self\.)?color_stacks\s*=\s*\{")
    findings: list[dict] = []
    for path in sorted(SCAN_ROOT.rglob("*.py")):
        rel = str(path.relative_to(REPO))
        if "__pycache__" in rel:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            continue
        for lineno, line in enumerate(lines, 1):
            if pattern.search(line):
                findings.append({
                    "file": rel, "line": lineno,
                    "code": line.strip()[:120],
                    "class": _LEGACY_WRITE_CLASSES.get(rel, "待审计"),
                })
    return findings


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(
        REPO / "docs/validation/color_nodes/capability_inventory.md"))
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    from src.app.controllers.color_graph_controller import ACTION_CONTRACTS
    from src.features.shared.effects.effect_catalog import (
        get_effect_catalog,
        graph_backend_capability_matrix,
        graph_node_backend_options,
    )
    from src.features.color.core.color_graph_service import ColorGraphService

    findings = _scan_legacy_writes()
    violations = [f for f in findings if f["class"] == "待审计"]

    catalog = get_effect_catalog()
    specs = catalog.get_all_specs()

    lines = [
        "# 能力汇总与旧 layers 写入口清单（B16.1）", "",
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}；"
        f"git `{_git_sha()}`",
        "- action 表：B01 契约（UI envelope → service command）；"
        "provider 表：EffectCatalog（效果注册）；backend 表：图算子三"
        "后端矩阵（B12.1，实测见 backend_matrix.md）", "",
        f"## 1. Action 表（{len(ACTION_CONTRACTS)} 项）", "",
        "| action | category | command |", "|---|---|---|",
    ]
    for name in sorted(ACTION_CONTRACTS):
        contract = ACTION_CONTRACTS[name]
        category = getattr(contract.category, "value", contract.category)
        lines.append(f"| {name} | {category} | {contract.command} |")

    lines += ["", "## 2. Provider 表（EffectCatalog，%d 效果）" % len(specs),
              "", "| effect | category | shader kind |", "|---|---|---|"]
    for spec in sorted(specs, key=lambda s: (s.category, s.effect_type)):
        lines.append(f"| {spec.effect_type} | {spec.category} | "
                     f"{spec.shader_kind} |")

    matrix = graph_backend_capability_matrix()
    lines += ["", "## 3. Backend 表（图算子三后端）", "",
              "| op | gl | cpu | metal |", "|---|---|---|---|"]
    for op, backends in matrix.items():
        lines.append(f"| {op} | {backends['gl']} | {backends['cpu']} | "
                     f"{backends['metal']} |")
    options = graph_node_backend_options()
    disabled = [o["node_type"] for o in options if not o["ui_enabled"]]
    lines += ["", f"UI 可选项：{len(options)} 项；ui_enabled=False："
              f"{disabled or '无'}（provider 未实现如实禁用）"]

    lines += ["", "## 4. 旧 layers 写入口扫描（src/）", "",
              "| 文件:行 | 分类 | 代码 |", "|---|---|---|"]
    for f in findings:
        lines.append(f"| {f['file']}:{f['line']} | {f['class']} | "
                     f"`{f['code']}` |")
    lines += ["", f"**待审计：{len(violations)} 处**"
              + ("——已分类写点全部入账（迁移/投影/工程IO/过渡），"
                 "B16.5 切换前据此清单退场" if not violations else
                 "——存在未分类写点，需人工归类后重扫"), ""]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"written: {out}")
    print(f"findings={len(findings)} unclassified={len(violations)}")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "git": _git_sha(),
            "actions": {k: {"category": v.category, "command": v.command}
                        for k, v in ACTION_CONTRACTS.items()},
            "providers": [{"effect": s.effect_type,
                           "category": s.category,
                           "kind": s.shader_kind} for s in specs],
            "backends": matrix,
            "ui_disabled": disabled,
            "legacy_write_findings": findings,
        }, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"written: {args.json}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
