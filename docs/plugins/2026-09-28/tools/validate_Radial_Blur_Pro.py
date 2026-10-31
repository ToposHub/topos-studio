#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_Radial_Blur_Pro.py — headless 解析验证（计划03）

对 plugins/Radial_Blur_Pro.tglsl 做与
src/features/color/core/plugin_manager.py 一致的元数据解析（正则逐条镜像，
标注源码行号），并按 docs/plugins/2026-09-28/STYLE_NOTES.md §7 红线清单
做静态自检。不导入仓库代码（纯 stdlib，无 Qt / 无 OpenGL），可在任意
headless 环境运行：

    python3 docs/plugins/2026-09-28/tools/validate_Radial_Blur_Pro.py

退出码：0 = 全部通过；1 = 存在 FAIL。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_FILE = REPO_ROOT / "plugins" / "Radial_Blur_Pro.tglsl"
PLUGIN_ID = "Radial_Blur_Pro"

# ----------------------------------------------------------------------
# 镜像自 plugin_manager.py 的解析正则（行号为该文件当前实态）
# ----------------------------------------------------------------------
RX_NAME = re.compile(r"//\s*@name\s+(.+)")                      # L114
RX_I18N_NAME = re.compile(r"//\s*@i18n_name\s+(.+)")            # L118
RX_AUTHOR = re.compile(r"//\s*@author\s+(.+)")                  # L125
RX_VERSION = re.compile(r"//\s*@version\s+(.+)")                # L133
RX_DESCRIPTION = re.compile(r"//\s*@description\s+(.+)")        # L140
RX_PREVIEW_COST = re.compile(r"//\s*@preview_cost\s+(high|medium|low|skip)")  # L129
RX_PARAM = re.compile(r'//\s*@param\s+(\w+)\s+(\w+)\s+"([^"]+)"\s+(.+)')      # L288
RX_SELECT = re.compile(r"([\d.]+)\s+(.+)")                      # L338
RX_CLOSER = re.compile(r"//\s*-{5,}")                           # L461
CAPABILITY_KEYS = (
    "input_space", "output_space", "alpha", "precision",
    "determinism", "export_support", "threading",
    "graphics_backends", "compute_backends", "occlusion",
)                                                                # L182-193

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((ok, label if not detail else f"{label} — {detail}"))
    return ok


def parse_params(content: str) -> list[dict]:
    """复刻 plugin_manager._parse_params（L267-351）的数据结构。"""
    params = []
    for m in RX_PARAM.finditer(content):
        ptype, var, label, values = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        param = {"type": ptype, "var_name": var, "label": label}
        if ptype == "slider":
            parts = values.split()
            if len(parts) >= 3:
                param["default"], param["min"], param["max"] = (
                    float(parts[0]), float(parts[1]), float(parts[2]))
        elif ptype == "checkbox":
            param["default"] = int(float(values.split()[0]))
        elif ptype == "color":
            parts = values.split()
            param["default"] = ([float(x) for x in parts[:3]]
                                if len(parts) >= 3 else [1.0, 1.0, 1.0])
        elif ptype == "select":
            sm = RX_SELECT.match(values)
            if sm:
                param["default"] = int(float(sm.group(1)))
                param["options"] = re.findall(r'"([^"]+)"', sm.group(2))
        params.append(param)
    return params


def strip_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    return re.sub(r"//[^\n]*", " ", code)


def call_args_spans(code: str, fn: str) -> list[str]:
    """提取 code 中所有 fn(...) 调用的参数文本（括号配平，支持嵌套）。"""
    spans = []
    for m in re.finditer(rf"\b{fn}\s*\(", code):
        depth, i = 1, m.end()
        while i < len(code) and depth:
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
            i += 1
        if depth == 0:
            spans.append(code[m.end():i - 1])
    return spans


def main() -> int:
    if not check(PLUGIN_FILE.is_file(), "文件存在", str(PLUGIN_FILE.relative_to(REPO_ROOT))):
        report()
        return 1
    content = PLUGIN_FILE.read_text(encoding="utf-8")

    # ---- 红线 8：头部包裹行 + 元数据位置 -----------------------------
    closer = RX_CLOSER.search(content)
    check(closer is not None, "头部包裹行", "存在 //\\s*-{5,} 结束行")
    check(content.lstrip().startswith("// --- Topos Plugin Metadata ---"),
          "头部包裹行", "首行为 // --- Topos Plugin Metadata ---")
    directive_lines = [m for m in re.finditer(r"//\s*@\w+", content)]
    if closer:
        stray = [content[m.start():m.end()] for m in directive_lines if m.start() > closer.end()]
        check(not stray, "元数据位置", f"包裹行后无 @ 指令（发现 {stray[:3]}）" if stray
              else "全部 @ 指令位于 // ----------------------------- 之前")

    # ---- 头部字段 ----------------------------------------------------
    def first(rx):
        m = rx.search(content)
        return m.group(1).strip() if m else None

    check(first(RX_NAME) == "Radial Blur Pro", "@name", str(first(RX_NAME)))
    check(first(RX_I18N_NAME) == PLUGIN_ID, "@i18n_name == 文件名 stem", str(first(RX_I18N_NAME)))
    check(first(RX_AUTHOR) == "Topos Team", "@author", str(first(RX_AUTHOR)))
    check(first(RX_VERSION) == "1.0", "@version", str(first(RX_VERSION)))
    check(bool(first(RX_DESCRIPTION)), "@description", "单行英文描述存在"
          if first(RX_DESCRIPTION) else "缺失")

    caps = {}
    for key in CAPABILITY_KEYS:
        m = re.search(rf"//\s*@{key}\s+(\S+)", content)
        if m:
            caps[key] = m.group(1).strip()
    check(caps.get("input_space") == "scene-linear", "@input_space", str(caps.get("input_space")))
    check(caps.get("output_space") == "scene-linear", "@output_space", str(caps.get("output_space")))
    check(first(RX_PREVIEW_COST) in {"high", "medium", "low", "skip"},
          "@preview_cost", str(first(RX_PREVIEW_COST)))

    # ---- 参数表 ------------------------------------------------------
    params = parse_params(content)
    by_var = {p["var_name"]: p for p in params}
    check(len(params) == 4, "参数数量 = 4（select + 3 slider）", f"解析到 {len(params)} 个")

    sel = by_var.get("sample_quality", {})
    check(sel.get("type") == "select", "sample_quality 类型", str(sel.get("type")))
    check(sel.get("options") == ["8 Samples", "16 Samples", "24 Samples", "32 Samples"],
          "sample_quality 选项表", str(sel.get("options")))
    check(sel.get("default") == 1 and 0 <= sel.get("default", -1) < len(sel.get("options", [])),
          "sample_quality 默认索引", f"default={sel.get('default')}")

    for var, dflt in (("strength", 0.35), ("center_x", 0.5), ("center_y", 0.5)):
        p = by_var.get(var, {})
        ok = (p.get("type") == "slider" and p.get("default") == dflt
              and p.get("min") == 0.0 and p.get("max") == 1.0
              and p.get("min", 1.0) <= p.get("default", -1.0) <= p.get("max", -1.0))
        check(ok, f"{var} slider 契约", f"default={p.get('default')} range=[{p.get('min')},{p.get('max')}]")

    # ---- 着色器代码提取（复刻 _extract_shader_code L448-486） ---------
    shader_code = ""
    if closer:
        shader_code = content[closer.end():].strip()
    code_nc = strip_comments(shader_code)

    check("vec3 process(vec3 color, vec2 uv)" in shader_code,
          "process 入口签名", "vec3 process(vec3 color, vec2 uv)")
    has_code_line = any(ln.strip() and not ln.strip().startswith("//")
                        for ln in shader_code.splitlines())
    check(has_code_line, "代码提取", "包裹行之后含非注释代码行（非降级路径）")

    # ---- 红线 1：浮点显式 .0（裸整数仅限白名单语境） -----------------
    scratch = code_nc
    for pat in (
        r"RADIAL_ACCUMULATE\(\s*\d+\s*\)",          # 宏调用点字面量（整数语境）
        r"\bint\s+\w+\s*=\s*\d+\b",                 # 循环计数器初始化
        r"[=!<>]=\s*\d+\b",                         # 整数比较（quality == 0 等）
        r"textureSize\(\s*\w+\s*,\s*\d+\s*\)",      # textureSize lod
    ):
        scratch = re.sub(pat, "INT_OK", scratch)
    leftover = re.findall(r"(?<![\w.])\d+(?![\w.])", scratch)
    check(not leftover, "浮点显式 .0", f"残留裸整数 {leftover[:5]}" if leftover
          else "float 字面量均带小数点，裸整数仅在循环/比较/lod 语境")

    # ---- 红线 2：循环上限编译期常量 ----------------------------------
    macro_def = re.search(r"#define\s+RADIAL_ACCUMULATE\(\s*(\w+)\s*\)", shader_code)
    check(macro_def is not None, "宏定义", "#define RADIAL_ACCUMULATE(N)" if macro_def else "缺失")
    n_name = macro_def.group(1) if macro_def else "N"
    loops = re.findall(r"for\s*\(\s*int\s+\w+\s*=\s*\d+\s*;\s*\w+\s*<\s*(\w+)\s*;", code_nc)
    check(bool(loops) and all(bound == n_name for bound in loops),
          "循环上限为宏参（编译期字面量）", f"bounds={loops}")
    call_sites = [int(x) for x in re.findall(r"RADIAL_ACCUMULATE\(\s*(\d+)\s*\)", code_nc)]
    option_counts = [int(re.match(r"(\d+)", opt).group(1)) for opt in sel.get("options", [])]
    check(call_sites == option_counts,
          "select 选项 ↔ 宏分支字面量同序同值", f"branches={call_sites} options={option_counts}")
    check(call_sites == sorted(call_sites) and len(set(call_sites)) == len(call_sites),
          "分支字面量升序且唯一", str(call_sites))
    branch_tail = re.search(r"else\s*\{\s*RADIAL_ACCUMULATE\(\s*\d+\s*\)\s*\}", code_nc)
    check(branch_tail is not None, "末尾 else 兜底分支", "if 链最后分支为 else")

    # ---- 红线 3：无内容 clamp(0,1) -----------------------------------
    bad_clamps = [a for a in call_args_spans(code_nc, "clamp")
                  if re.search(r"\b0\.0\b", a) and re.search(r"\b1\.0\b", a)]
    check(not bad_clamps, "无 clamp(0,1) 于图像内容", f"发现 {bad_clamps[:2]}" if bad_clamps
          else "仅 UV 几何 clamp（半 texel 边界），内容值不 clamp")
    check("clamp(suv, min_uv, max_uv)" in shader_code, "UV clamp-to-edge 显式钳制",
          "clamp(suv, min_uv, max_uv)")

    # ---- 红线 4：UV 安全 / 分母守护 ----------------------------------
    check(re.search(r"max\s*\(\s*tex_size\s*,\s*vec2\s*\(\s*1\.0\s*,\s*1\.0\s*\)\s*\)",
                    code_nc) is not None, "分母下限守护", "0.5 / max(tex_size, vec2(1.0, 1.0))")
    check("textureSize(u_input_tex, 0)" in code_nc, "分辨率实时获取", "textureSize(u_input_tex, 0)")

    # ---- 红线 5：性能 -----------------------------------------------
    proc_body = re.search(r"vec3\s+process\s*\([^)]*\)\s*\{", code_nc)
    first_stmt = code_nc[proc_body.end():proc_body.end() + 80] if proc_body else ""
    check(re.match(r"\s*if\s*\(\s*strength\s*<\s*0\.001\s*\)\s*return\s+color\s*;", first_stmt)
          is not None, "process 首行早退出", "if (strength < 0.001) return color;")
    check("return color + (blurred - original_color);" in code_nc, "delta 输出模式",
          "return color + (blurred - original_color);")
    expensive = {fn: len(re.findall(rf"\b{fn}\s*\(", code_nc)) for fn in ("sin", "cos", "exp", "log")}
    expensive["pow"] = len(re.findall(r"\bpow\s*\(", code_nc))
    check(expensive["pow"] <= 1 and not any(expensive[k] for k in ("sin", "cos", "exp", "log")),
          "昂贵函数用量", f"pow×{expensive['pow']}，sin/cos/exp/log×0（tap 数 = N ≤ 32 由 select 控制）")

    # ---- 红线 6：无品牌商标名 ---------------------------------------
    trademarks = [t for t in ("ARRI", "Sony", "RED", "DaVinci", "Photoshop",
                              "Lightroom", "Capture One", "Final Cut")
                  if re.search(rf"\b{re.escape(t)}\b", content)]
    check(not trademarks, "无品牌商标名", str(trademarks) if trademarks else "全文件未命中商标词表")

    # ---- 红线 7：无未声明 u_* 引用 -----------------------------------
    allowed_u = {"u_input_tex", "u_tex_size", "u_aspect_ratio"}
    used_u = sorted(set(re.findall(r"\bu_\w+", code_nc)))
    check(set(used_u) <= allowed_u, "u_* 白名单", f"used={used_u} ⊆ {sorted(allowed_u)}")

    # ---- 参数裸名均被引用（无死参数） --------------------------------
    dead = [p["var_name"] for p in params
            if not re.search(rf"\b{re.escape(p['var_name'])}\b", code_nc)]
    check(not dead, "参数全部被着色器引用", f"死参数 {dead}" if dead else "4/4 裸名引用")

    report()
    return 0 if all(ok for ok, _ in RESULTS) else 1


def report() -> None:
    print(f"validate_Radial_Blur_Pro — {PLUGIN_FILE.relative_to(REPO_ROOT)}")
    print("-" * 78)
    passed = 0
    for ok, line in RESULTS:
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {line}")
    print("-" * 78)
    print(f"结果: {passed}/{len(RESULTS)} 通过, {len(RESULTS) - passed} 失败")
    print("退出码: " + ("0 (ALL PASS)" if passed == len(RESULTS) else "1 (HAS FAILURES)"))


if __name__ == "__main__":
    sys.exit(main())
