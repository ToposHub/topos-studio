#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_Vignette_Shapes.py — headless 解析验证（计划03）

对 plugins/Vignette_Shapes.tglsl 做与
src/features/color/core/plugin_manager.py 一致的元数据解析（正则逐条镜像，
标注源码行号），按 docs/plugins/2026-09-28/STYLE_NOTES.md §7 红线清单做
静态自检，并在纯 stdlib 数值仿真中逐像素复算着色器数学，验证：
  - E[t] 闭合式标定 vs 逐点数值积分（3 形状 × 3 宽高比）；
  - 亮度补偿 comp=1 时的均值保持（|mean - 1| <= 0.06）；
  - comp=0 时中心像素不变、角点暗化 = 1 - strength、strength=0 直通。
不导入仓库代码（无 Qt / 无 OpenGL / 无 numpy），可在任意 headless 环境运行：

    python3 docs/plugins/2026-09-28/tools/validate_Vignette_Shapes.py

退出码：0 = 全部通过；1 = 存在 FAIL。
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_FILE = REPO_ROOT / "plugins" / "Vignette_Shapes.tglsl"
PLUGIN_ID = "Vignette_Shapes"

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
RX_CLOSER = re.compile(r"//\s*-{5,}")                           # L460
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


# ----------------------------------------------------------------------
# 着色器数学的 Python 逐行镜像（数值验证用；与 .tglsl 保持同构）。
# falloff_mean 的标定常数在 main() 中从着色器文本正则提取并写入 COEFFS，
# 因此数值验证校验的是 .tglsl 里实际写着的常数（防"镜像与实现脱钩"）。
# ----------------------------------------------------------------------
COEFFS: dict[str, float] = {}   # 由 parse_coeffs() 填充


def smoothstep_py(e0: float, e1: float, x: float) -> float:
    t = min(max((x - e0) / (e1 - e0), 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def rounded_box_sdf_py(px: float, py: float, bx: float, by: float, r: float) -> float:
    qx, qy = abs(px) - bx + r, abs(py) - by + r
    lx, ly = max(qx, 0.0), max(qy, 0.0)
    return math.hypot(lx, ly) + min(max(qx, qy), 0.0) - r


def shape_distance_py(qx: float, qy: float, aspect: float,
                      corner_r: float, idx: int) -> float:
    if idx == 0:
        return math.hypot(qx, qy)
    if idx == 1:
        return 1.0 + rounded_box_sdf_py(qx * aspect, qy, aspect, 1.0, corner_r)
    return math.hypot(qx * aspect, qy)


def falloff_mean_py(idx: int, w: float, aspect: float) -> float:
    if idx == 0:
        return COEFFS["ell_a"] + COEFFS["ell_b"] * w
    if idx == 1:
        return COEFFS["rect_k"] * (1.0 + 1.0 / max(aspect, COEFFS["rect_floor"])) * w
    if aspect >= 1.0:
        hard = max(1.0 - COEFFS["cir_pi4"] / max(aspect, COEFFS["cir_floor"]), 0.0)
    else:
        hard = max(COEFFS["cir_pa"] - COEFFS["cir_pb"] * (1.0 - aspect), 0.0)
    slope = (COEFFS["cir_s0"] + COEFFS["cir_s1"] * max(1.0 - aspect, 0.0)
             - COEFFS["cir_s2"] * max(aspect - 1.0, 0.0))
    return hard + max(slope, COEFFS["cir_sfloor"]) * w


def parse_coeffs(code_nc: str) -> bool:
    """从着色器代码（注释已剥离）提取 falloff_mean 的全部标定常数。"""
    pats: list[tuple[str, re.Pattern]] = [
        ("ell_a", re.compile(r"return\s+(0\.\d+)\s*\+\s*(0\.\d+)\s*\*\s*w\s*;")),
        ("rect_k", re.compile(
            r"return\s+(0\.\d+)\s*\*\s*\(1\.0\s*\+\s*1\.0\s*/\s*max\(aspect\s*,\s*(0\.\d+)\)\)\s*\*\s*w")),
        ("cir_pi4", re.compile(
            r"max\(1\.0\s*-\s*(0\.\d+)\s*/\s*max\(aspect\s*,\s*(0\.\d+)\)")),
        ("cir_pa", re.compile(
            r"max\((0\.\d+)\s*-\s*(0\.\d+)\s*\*\s*\(1\.0\s*-\s*aspect\)")),
        ("cir_s0", re.compile(
            r"(0\.\d+)\s*\+\s*(0\.\d+)\s*\*\s*max\(1\.0\s*-\s*aspect\s*,\s*0\.0\)\s*"
            r"-\s*(0\.\d+)\s*\*\s*max\(aspect\s*-\s*1\.0\s*,\s*0\.0\)")),
        ("cir_sfloor", re.compile(r"max\(slope\s*,\s*(0\.\d+)\)")),
    ]
    try:
        m = pats[0][1].search(code_nc)
        COEFFS["ell_a"], COEFFS["ell_b"] = float(m.group(1)), float(m.group(2))
        m = pats[1][1].search(code_nc)
        COEFFS["rect_k"], COEFFS["rect_floor"] = float(m.group(1)), float(m.group(2))
        m = pats[2][1].search(code_nc)
        COEFFS["cir_pi4"], COEFFS["cir_floor"] = float(m.group(1)), float(m.group(2))
        m = pats[3][1].search(code_nc)
        COEFFS["cir_pa"], COEFFS["cir_pb"] = float(m.group(1)), float(m.group(2))
        m = pats[4][1].search(code_nc)
        COEFFS["cir_s0"], COEFFS["cir_s1"], COEFFS["cir_s2"] = (
            float(m.group(1)), float(m.group(2)), float(m.group(3)))
        m = pats[5][1].search(code_nc)
        COEFFS["cir_sfloor"] = float(m.group(1))
        return True
    except (AttributeError, ValueError):
        return False


def simulate(idx: int, aspect: float, feather: float, strength: float,
             comp: float, n: int = 420) -> tuple[float, float, float, float]:
    """逐像素复算 process 的标量数学（常数图像上），返回
    (E_t 数值, E[weight]*gain 数值, 中心输出比, 角点输出比)。"""
    w = 0.08 + (0.55 - 0.08) * feather          # mix(0.08, 0.55, feather)
    corner_r = 0.9 * w
    acc_t, acc_out = 0.0, 0.0
    for iy in range(n):
        v = (iy + 0.5) / n
        qy = (v - 0.5) * 2.0
        for ix in range(n):
            u = (ix + 0.5) / n
            qx = (u - 0.5) * 2.0
            d = shape_distance_py(qx, qy, aspect, corner_r, idx)
            t = smoothstep_py(1.0 - w, 1.0 + w, d)
            acc_t += t
            weight = 1.0 - strength * t
            mean_t = falloff_mean_py(idx, w, aspect)
            mean_weight = max(1.0 - strength * mean_t, 0.25)
            gain = 1.0 + (1.0 / mean_weight - 1.0) * comp   # mix(1, g, comp)
            acc_out += weight * gain
    e_t = acc_t / (n * n)
    e_out = acc_out / (n * n)
    # 中心（uv=0.5,0.5）与角点（uv=1,1）的单点输出比
    d_c = shape_distance_py(0.0, 0.0, aspect, corner_r, idx)
    t_c = smoothstep_py(1.0 - w, 1.0 + w, d_c)
    d_k = shape_distance_py(1.0, 1.0, aspect, corner_r, idx)
    t_k = smoothstep_py(1.0 - w, 1.0 + w, d_k)
    mean_t = falloff_mean_py(idx, w, aspect)
    mean_weight = max(1.0 - strength * mean_t, 0.25)
    gain = 1.0 + (1.0 / mean_weight - 1.0) * comp
    return e_t, e_out, (1.0 - strength * t_c) * gain, (1.0 - strength * t_k) * gain


SHAPES = ("ellipse", "rect", "circle")
ASPECTS = (1.0, 16.0 / 9.0, 9.0 / 16.0)


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

    check(first(RX_NAME) == "Vignette Shapes", "@name", str(first(RX_NAME)))
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
    check(caps.get("determinism") == "deterministic", "@determinism", str(caps.get("determinism")))
    check(first(RX_PREVIEW_COST) in {"high", "medium", "low", "skip"},
          "@preview_cost", str(first(RX_PREVIEW_COST)))

    # ---- 参数表 ------------------------------------------------------
    params = parse_params(content)
    by_var = {p["var_name"]: p for p in params}
    check(len(params) == 4, "参数数量 = 4（select + 3 slider）", f"解析到 {len(params)} 个")

    sel = by_var.get("shape", {})
    check(sel.get("type") == "select", "shape 类型", str(sel.get("type")))
    check(sel.get("options") == ["Ellipse", "Rectangle", "Circle"],
          "shape 选项表", str(sel.get("options")))
    check(sel.get("default") == 0 and 0 <= sel.get("default", -1) < len(sel.get("options", [])),
          "shape 默认索引", f"default={sel.get('default')}")

    for var, dflt in (("feather", 0.5), ("strength", 0.5), ("luma_comp", 0.0)):
        p = by_var.get(var, {})
        ok = (p.get("type") == "slider" and p.get("default") == dflt
              and p.get("min") == 0.0 and p.get("max") == 1.0
              and p.get("min", 1.0) <= p.get("default", -1.0) <= p.get("max", -1.0))
        check(ok, f"{var} slider 契约", f"default={p.get('default')} range=[{p.get('min')},{p.get('max')}]")

    # ---- 着色器代码提取（复刻 _extract_shader_code L448-486） ---------
    shader_code = content[closer.end():].strip() if closer else ""
    code_nc = strip_comments(shader_code)

    check("vec3 process(vec3 color, vec2 uv)" in shader_code,
          "process 入口签名", "vec3 process(vec3 color, vec2 uv)")
    has_code_line = any(ln.strip() and not ln.strip().startswith("//")
                        for ln in shader_code.splitlines())
    check(has_code_line, "代码提取", "包裹行之后含非注释代码行（非降级路径）")

    # ---- 红线 1：浮点显式 .0（裸整数仅限白名单语境） -----------------
    scratch = code_nc
    for pat in (
        r"[=!<>]=\s*\d+\b",                         # 整数比较（shape_idx == 0 等）
        r"\bint\s+\w+\s*=\s*\d+\b",                 # int 初始化（本插件无循环，防御性保留）
    ):
        scratch = re.sub(pat, "INT_OK", scratch)
    leftover = re.findall(r"(?<![\w.])\d+(?![\w.])", scratch)
    check(not leftover, "浮点显式 .0", f"残留裸整数 {leftover[:5]}" if leftover
          else "float 字面量均带小数点，裸整数仅在 select 索引比较语境")

    # ---- 红线 2：循环上限编译期常量 ----------------------------------
    check("for" not in re.findall(r"\bfor\b", code_nc), "零 for 循环",
          "纯逐像素数学，循环上限红线 vacuously 成立" if "for" not in code_nc
          else f"发现 for 循环：{re.findall(r'for[^;]{0,40}', code_nc)[:2]}")

    # ---- select 分支结构（早返回 if 链 + else 兜底） ------------------
    n_head = len(re.findall(r"if\s*\(\s*shape_idx\s*==\s*0\s*\)", code_nc))
    n_mid = len(re.findall(r"else\s+if\s*\(\s*shape_idx\s*==\s*1\s*\)", code_nc))
    n_else = len(re.findall(r"\}\s*else\s*\{", code_nc))
    check(n_head == 2 and n_mid == 2 and n_else >= 2,
          "shape 分支 if 链",
          f"if(==0)×{n_head} / else if(==1)×{n_mid} / else 兜底×{n_else}"
          f"（shape_distance + falloff_mean 各一条，末分支 else = circle）")
    n_calls = len(re.findall(r"\bshape_distance\s*\(", code_nc)) - 1   # 减去定义行
    n_fmean = len(re.findall(r"\bfalloff_mean\s*\(", code_nc)) - 1
    check(n_calls >= 1 and n_fmean >= 1, "分发函数调用点",
          f"shape_distance×{n_calls} falloff_mean×{n_fmean}（select 索引作参数传入）")

    # ---- 红线 3：无内容 clamp(0,1) -----------------------------------
    clamps = call_args_spans(code_nc, "clamp")
    bad_clamps = [a for a in clamps if re.search(r"\b0\.0\b", a) and re.search(r"\b1\.0\b", a)]
    check(not clamps, "零 clamp 调用", f"发现 clamp({clamps[:1]}...)" if clamps else
          ("含 clamp(0,1) 于图像内容" if bad_clamps else "全文件无 clamp，内容值不裁切"))

    # ---- 红线 4：UV/分母安全 + 径向纠偏 ------------------------------
    check(re.search(r"max\s*\(\s*aspect\s*,\s*0\.1\s*\)", code_nc) is not None,
          "分母下限守护", "max(aspect, 0.1)（rect/circle 分母）")
    check("max(1.0 - strength * mean_t, 0.25)" in code_nc, "增益分母守护",
          "mean_weight 下限 0.25（gain <= 4.0）")
    check("mix(0.08, 0.55, feather)" in code_nc, "smoothstep 边距下限",
          "w = mix(0.08, 0.55, feather)，edge0 < edge1 恒成立")
    aspect_fixes = len(re.findall(r"q\.x\s*\*\s*aspect", code_nc))
    check(aspect_fixes >= 2, "径向纠偏 u_aspect_ratio",
          f"q.x * aspect 出现 {aspect_fixes} 处（circle 度量 + rect SDF 空间）")

    # ---- 红线 5：性能 -----------------------------------------------
    proc_body = re.search(r"vec3\s+process\s*\([^)]*\)\s*\{", code_nc)
    first_stmt = code_nc[proc_body.end():proc_body.end() + 80] if proc_body else ""
    check(re.match(r"\s*if\s*\(\s*strength\s*<\s*0\.001\s*\)\s*return\s+color\s*;", first_stmt)
          is not None, "process 首行早退出", "if (strength < 0.001) return color;")
    check(not re.search(r"\btexture\s*\(", code_nc), "零纹理采样",
          "纯 uv 数学，无 texture() tap（对比 HighPass_Retouch 的 2 tap）")
    expensive = {fn: len(re.findall(rf"\b{fn}\s*\(", code_nc))
                 for fn in ("sin", "cos", "exp", "log", "pow")}
    check(not any(expensive.values()), "昂贵函数用量",
          f"sin/cos/exp/log/pow 总计 ×{sum(expensive.values())}（仅 mix/max/min/length/smoothstep）")

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

    # ---- 数值验证前置：标定常数提取自着色器文本 ----------------------
    ok_coeff = parse_coeffs(code_nc)
    coeff_detail = ("，".join(f"{k}={v:g}" for k, v in sorted(COEFFS.items()))
                    if ok_coeff else "falloff_mean 常数正则未全部命中")
    check(ok_coeff and len(COEFFS) == 12, "标定常数提取自着色器文本", coeff_detail)
    if not ok_coeff:
        report()
        return 1

    # ---- 数值验证：E[t] 闭合式 vs 逐点积分 ---------------------------
    worst_et = 0.0
    detail_et = ""
    for si, sname in enumerate(SHAPES):
        for aspect in ASPECTS:
            e_t_num, _, _, _ = simulate(si, aspect, 0.5, 0.5, 0.0, n=360)
            e_t_formula = falloff_mean_py(si, 0.08 + 0.47 * 0.5, aspect)
            delta = abs(e_t_num - e_t_formula)
            if delta > worst_et:
                worst_et, detail_et = delta, f"{sname}@{aspect:.3f}: 数值 {e_t_num:.4f} vs 闭合式 {e_t_formula:.4f}"
    check(worst_et <= 0.05, "E[t] 标定精度（默认羽化, 3 形状 × 3 宽高比）",
          f"max|Δ|={worst_et:.4f}（{detail_et}）<= 0.05")

    # ---- 数值验证：亮度补偿的均值保持 --------------------------------
    worst_mean = 0.0
    detail_mean = ""
    for si, sname in enumerate(SHAPES):
        for aspect in ASPECTS:
            _, e_out, _, _ = simulate(si, aspect, 0.5, 1.0, 1.0, n=360)
            dev = abs(e_out - 1.0)
            if dev > worst_mean:
                worst_mean, detail_mean = dev, f"{sname}@{aspect:.3f}: mean={e_out:.4f}"
    check(worst_mean <= 0.06, "均值保持（strength=1, comp=1）",
          f"max|mean-1|={worst_mean:.4f}（{detail_mean}）<= 0.06")

    # ---- 数值验证：行为契约 ------------------------------------------
    _, _, center_ratio, corner_ratio = simulate(0, 16.0 / 9.0, 0.5, 0.5, 0.0, n=32)
    ok_c = abs(center_ratio - 1.0) < 1e-12
    check(ok_c, "comp=0 中心像素不变", f"center 输出比 = {center_ratio:.12f}")
    ok_k = abs(corner_ratio - (1.0 - 0.5)) < 1e-3   # 椭圆角点 d=√2 ≥ 1+w → t=1
    check(ok_k, "comp=0 角点暗化 = 1 - strength", f"corner 输出比 = {corner_ratio:.4f}（期望 0.5000）")

    report()
    return 0 if all(ok for ok, _ in RESULTS) else 1


def report() -> None:
    print(f"validate_Vignette_Shapes — {PLUGIN_FILE.relative_to(REPO_ROOT)}")
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
