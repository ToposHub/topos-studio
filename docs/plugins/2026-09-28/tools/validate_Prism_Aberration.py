#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_Prism_Aberration.py — headless 解析验证（计划03）

验证对象：
  plugins/Prism_Aberration.tglsl
  docs/plugins/2026-09-28/i18n/Prism_Aberration.json

验证内容：
  1. 真实解析器路径：优先调用 src/features/color/core/plugin_manager.py 的
     PluginManager._parse_plugin_file（与运行时完全同源，headless 导入）；
     导入失败时回退到本文件内嵌的同源正则复刻。
  2. 元数据契约：包裹行、@name/@i18n_name/@author/@version/@description、
     capability manifest、@preview_cost、3 个 slider 参数的类型/默认/范围。
  3. GLSL 红线（STYLE_NOTES §7）：float 字面量带 .0、无 uniform 循环上限
     （本插件无循环）、无未声明 u_* 引用、无内容 clamp(0,1)、无品牌商标名、
     UV clamp-to-edge、分母守护、径向乘 u_aspect_ratio、首行早退出、
     delta 输出、昂贵函数与纹理 tap 用量、RGB 三通道分离采样结构。
  4. i18n 片段结构与 .tglsl Label 的一致性（zh_CN + en_US）。

用法：python3 docs/plugins/2026-09-28/tools/validate_Prism_Aberration.py
退出码：0 = 全部通过；1 = 存在 FAIL。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# 路径解析：tools -> 2026-09-28 -> plugins(docs) -> docs -> 仓库根
# ----------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_PATH = REPO_ROOT / "plugins" / "Prism_Aberration.tglsl"
I18N_PATH = Path(__file__).resolve().parents[1] / "i18n" / "Prism_Aberration.json"

PLUGIN_ID = "Prism_Aberration"
EXPECTED_PARAMS = [
    ("slider", "separation", "Dispersion Strength", 0.35, 0.0, 1.0),
    ("slider", "rotation", "Rotation (Degrees)", 0.0, -180.0, 180.0),
    ("slider", "edge_falloff", "Edge Falloff", 0.5, 0.0, 1.0),
]
ALLOWED_U_IDENTS = {"u_input_tex", "u_tex_size", "u_aspect_ratio"}
CAPABILITY_KEYS = (
    "input_space", "output_space", "alpha", "precision", "determinism",
    "export_support", "threading", "graphics_backends", "compute_backends",
    "occlusion",
)
BRAND_PATTERNS = [
    r"\bARRI\b", r"\bSony\b", r"\bRED\b", r"\bDaVinci\b", r"\bPhotoshop\b",
    r"\bLightroom\b", r"\bCapture\s+One\b", r"\bPremiere\b", r"\bFinal\s+Cut\b",
]

results: List[Tuple[str, str]] = []  # (level, message)


def check(ok: bool, msg: str, warn_only: bool = False) -> bool:
    level = "PASS" if ok else ("WARN" if warn_only else "FAIL")
    results.append((level, msg))
    return ok


# ----------------------------------------------------------------------
# 内嵌解析复刻（plugin_manager.py 同源正则；仅在真实解析器不可导入时使用）
# ----------------------------------------------------------------------
def fallback_parse(content: str) -> Dict[str, Any]:
    def first(pattern: str) -> str:
        m = re.search(pattern, content)
        return m.group(1).strip() if m else ""

    params = []
    for m in re.finditer(r'//\s*@param\s+(\w+)\s+(\w+)\s+"([^"]+)"\s+(.+)', content):
        ptype, var, label, values = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        param: Dict[str, Any] = {"type": ptype, "var_name": var, "label": label}
        if ptype == "slider":
            parts = values.split()
            param["default"], param["min"], param["max"] = (
                float(parts[0]), float(parts[1]), float(parts[2]))
        params.append(param)

    capabilities = {
        k: v.group(1).strip()
        for k in CAPABILITY_KEYS
        for v in [re.search(rf"//\s*@{k}\s+(\S+)", content)] if v
    }

    _pc = re.search(r"//\s*@preview_cost\s+(high|medium|low|skip)", content)
    end = re.search(r"//\s*-{5,}", content)
    shader_code = content[end.end():].strip() if end else ""

    return {
        "plugin_id": PLUGIN_ID,
        "name": first(r"//\s*@name\s+(.+)") or PLUGIN_ID,
        "author": first(r"//\s*@author\s+(.+)") or "Unknown",
        "version": first(r"//\s*@version\s+(.+)"),
        "description": first(r"//\s*@description\s+(.+)"),
        "preview_cost": _pc.group(1).strip() if _pc else None,
        "params": params,
        "capabilities": capabilities,
        "shader_code": shader_code,
    }


def real_parse() -> Tuple[Optional[Dict[str, Any]], str]:
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from src.features.color.core.plugin_manager import PluginManager  # type: ignore
        pm = PluginManager()
        data = pm._parse_plugin_file(str(PLUGIN_PATH), PLUGIN_ID)
        # 与 .tglsl 原文无关的运行时字段不参与比对
        for k in ("source_uri", "content_hash", "type", "preview_quality_level"):
            data.pop(k, None)
        return data, "src.features.color.core.plugin_manager.PluginManager（真实解析器）"
    except Exception as e:  # noqa: BLE001 - 回退到内嵌复刻
        return None, f"真实解析器导入/执行失败（{type(e).__name__}: {e}），已回退内嵌复刻"


# ----------------------------------------------------------------------
# GLSL 红线静态检查
# ----------------------------------------------------------------------
def strip_comments(code: str) -> str:
    return "\n".join(re.sub(r"//.*$", "", line) for line in code.splitlines())


def call_args_spans(code: str, fn: str) -> List[str]:
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


def check_shader_redlines(shader_code: str, raw_content: str) -> None:
    code = strip_comments(shader_code)

    # 入口契约 / 核心惯例
    check("vec3 process(vec3 color, vec2 uv)" in code, "process 入口签名存在")
    check(re.search(r"vec3\s+process[^{]*\{\s*if\s*\(\s*separation\s*<\s*0\.001\s*\)\s*return\s+color\s*;",
                    code) is not None, "首行早退出（separation < 0.001）")
    check("return color + (dispersed - original_color);" in code,
          "delta 输出模式（return color + (dispersed - original_color);）")

    # RGB 三通道分离采样结构：G 原位参考，R/B 沿轴 +/- 偏移
    check(re.search(r"dispersed\.r\s*=\s*texture\s*\(\s*u_input_tex\s*,\s*clamp\s*\(\s*uv\s*\+\s*offset_uv",
                    code) is not None, "R 通道 = uv + offset_uv 采样")
    check(re.search(r"dispersed\.b\s*=\s*texture\s*\(\s*u_input_tex\s*,\s*clamp\s*\(\s*uv\s*-\s*offset_uv",
                    code) is not None, "B 通道 = uv - offset_uv 采样")
    check(re.search(r"dispersed\.g\s*=", code) is None,
          "G 通道未重采样（dispersed.g 沿用原位参考通道）")
    check(re.search(r"vec3\s+dispersed\s*=\s*original_color\s*;", code) is not None,
          "dispersed 以 original_color 为基底（G 原位）")

    # 红线 2：循环上限编译期常量 —— 本插件为 3-tap 直采，断言零 for 循环
    loops = re.findall(r"for\s*\(", code)
    check(not loops, f"无 for 循环（{len(loops)} 个；3-tap 直采天然满足编译期上限红线）")

    # 红线 1：float 字面量带 .0（裸整数仅限白名单整数语境）
    safe_int_contexts = [
        r"textureSize\s*\(\s*u_input_tex\s*,\s*\d+\s*\)",  # textureSize(..., 0)
    ]
    bare_int_hits = []
    for lineno, line in enumerate(code.splitlines(), 1):
        if not line.strip():
            continue
        for m in re.finditer(r"(?<![\w.])\d+(?![\w.])", line):
            ctx = line.strip()
            if not any(re.search(p, ctx) for p in safe_int_contexts):
                bare_int_hits.append(f"第 {lineno} 行: '{ctx}' 中的裸整数 '{m.group(0)}'")
    check(not bare_int_hits,
          "float 语境无缺失 .0 的裸整数" + ("" if not bare_int_hits else " -> " + "; ".join(bare_int_hits)))

    # 红线 3：无内容 clamp(0,1)；本插件仅允许 2 处 UV 几何 clamp（clamp-to-edge）
    clamp_spans = call_args_spans(code, "clamp")
    clamp01 = [a for a in clamp_spans if re.search(r"\b0\.0\b", a) and re.search(r"\b1\.0\b", a)]
    check(not clamp01, f"无 clamp(0,1)（HDR 红线）" + (f" -> 发现 {clamp01[:2]}" if clamp01 else ""))
    uv_clamps = [a for a in clamp_spans if "uv" in a and "min_uv" in a and "max_uv" in a]
    check(len(clamp_spans) == 2 and len(uv_clamps) == 2,
          f"clamp 仅 2 处且均为 UV 几何钳制（实际 clamp={len(clamp_spans)}, UV={len(uv_clamps)}）")

    # 红线 4：UV / 分母守护 / 径向纵横比纠偏
    check("0.5 / max(tex_size, vec2(1.0, 1.0))" in code, "半 texel 分母下限守护 max(tex_size, vec2(1.0, 1.0))")
    check(re.search(r"max\s*\(\s*u_aspect_ratio\s*,\s*0\.001\s*\)", code) is not None,
          "aspect 分母下限守护 max(u_aspect_ratio, 0.001)")
    check("radial / max(dist, 0.0001)" in code, "中心退化守护 radial / max(dist, 0.0001)")
    check("dist / max(max_radius, 0.0001)" in code, "半径分母守护 dist / max(max_radius, 0.0001)")
    check("textureSize(u_input_tex, 0)" in code, "分辨率实时获取 textureSize(u_input_tex, 0)")
    check(re.search(r"vec2\s*\(\s*uv\.x\s*\*\s*aspect\s*,\s*uv\.y\s*\)", code) is not None,
          "径向几何乘 u_aspect_ratio 纠偏（uv.x * aspect）")
    check(re.search(r"pow\s*\(\s*max\s*\(\s*dist01\s*,\s*0\.0001\s*\)", code) is not None,
          "pow 基数守护 max(dist01, 0.0001)（防 pow(0, 0) 未定义）")

    # 红线 5：性能预算
    taps = len(re.findall(r"\btexture\s*\(", code))
    check(taps == 3, f"纹理 tap 数 = {taps}（期望 3：参考 + R + B）")
    expensive = {fn: len(re.findall(rf"\b{fn}\s*\(", code)) for fn in ("sin", "cos", "exp", "log")}
    expensive["pow"] = len(re.findall(r"\bpow\s*\(", code))
    check(expensive == {"sin": 1, "cos": 1, "exp": 0, "log": 0, "pow": 2},
          f"昂贵函数用量 sin×{expensive['sin']} cos×{expensive['cos']} pow×{expensive['pow']} "
          f"exp/log×{expensive['exp']}/{expensive['log']}（期望 1/1/2/0/0）")

    # 红线 7：未声明 u_* 引用（词边界；重命名机制不覆盖 u_ 前缀，P0-COL-01）
    used_u = set(re.findall(r"\bu_\w+\b", code))
    bad_u = used_u - ALLOWED_U_IDENTS
    check(not bad_u, f"无未声明 u_* 引用（实际引用: {sorted(used_u) or '无'}）"
          + ("" if not bad_u else f" -> 非法: {sorted(bad_u)}"))

    # 花括号/圆括号配平（注释已剥离）
    check(code.count("{") == code.count("}"), f"花括号配平（{code.count('{')}/{code.count('}')}）")
    check(code.count("(") == code.count(")"), f"圆括号配平（{code.count('(')}/{code.count(')')}）")

    # 红线 6：品牌商标名（对全文含注释/元数据检查）
    for pat in BRAND_PATTERNS:
        hits = re.findall(pat, raw_content, flags=re.IGNORECASE)
        check(not hits, f"无品牌商标名 {pat}" + ("" if not hits else f" -> 命中 {hits}"))

    # 参数裸名均被引用（无死参数）
    for _, var, _, _, _, _ in EXPECTED_PARAMS:
        check(re.search(rf"\b{re.escape(var)}\b", code) is not None, f"参数 '{var}' 裸名被着色器引用")


# ----------------------------------------------------------------------
# i18n 片段检查
# ----------------------------------------------------------------------
def check_i18n() -> None:
    if not check(I18N_PATH.is_file(), f"i18n 片段存在（{I18N_PATH.name}）"):
        return
    data = json.loads(I18N_PATH.read_text(encoding="utf-8"))
    for locale in ("zh_CN", "en_US"):
        node = data.get(locale, {}).get("plugins", {}).get(PLUGIN_ID, {})
        check(bool(node.get("name")), f"[{locale}] plugins.{PLUGIN_ID}.name = '{node.get('name')}'")
        params = node.get("params", {})
        for _, var, label, _, _, _ in EXPECTED_PARAMS:
            check(bool(params.get(var)),
                  f"[{locale}] plugins.{PLUGIN_ID}.params.{var} = '{params.get(var)}'")
        if locale == "en_US":
            for (_, var, label, _, _, _) in EXPECTED_PARAMS:
                check(params.get(var) == label,
                      f"[{locale}] params.{var} 与 .tglsl Label 一致（'{params.get(var)}' == '{label}'）")
        desc_params = node.get("desc", {}).get("params", {})
        check(all(desc_params.get(v) for _, v, *_ in EXPECTED_PARAMS),
              f"[{locale}] desc.params 前向命名空间齐备")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main() -> int:
    if not check(PLUGIN_PATH.is_file(), f"文件存在 — {PLUGIN_PATH.relative_to(REPO_ROOT)}"):
        report()
        return 1
    content = PLUGIN_PATH.read_text(encoding="utf-8")

    # 红线 8：头部包裹行 + 元数据位置
    check(content.startswith("// --- Topos Plugin Metadata ---"),
          "头部首行包裹注释 // --- Topos Plugin Metadata ---")
    closer_m = re.search(r"//\s*-{5,}", "\n".join(content.split("\n")[:30]))
    check(closer_m is not None, "头部含闭合包裹行 // -----------------------------")
    if closer_m:
        stray = [m.group(0) for m in re.finditer(r"//\s*@\w+", content[closer_m.end():])]
        check(not stray, "元数据位置 — 全部 @ 指令位于包裹行之前"
              + (f"（发现越界 {stray[:3]}）" if stray else ""))

    # 真实解析器路径
    parsed_real, parser_src = real_parse()
    parsed = parsed_real or fallback_parse(content)
    check(parsed_real is not None, f"解析器: {parser_src}")

    # 元数据契约（i18n_name 不在解析器返回结构中，从原文提取比对）
    _i18n_m = re.search(r"//\s*@i18n_name\s+(.+)", content)
    i18n_name = _i18n_m.group(1).strip() if _i18n_m else ""
    check(parsed["name"] == "Prism Aberration", f"@name = '{parsed['name']}'")
    check(i18n_name == PLUGIN_ID, f"@i18n_name = '{i18n_name}'（= 文件名 stem）")
    check(parsed["author"] == "Topos Team", f"@author = '{parsed['author']}'")
    check(parsed["version"] == "1.0", f"@version = '{parsed['version']}'")
    check(bool(parsed["description"]), "@description 非空")
    check(parsed.get("preview_cost") == "low", f"@preview_cost = '{parsed.get('preview_cost')}'")

    caps = parsed["capabilities"]
    for key, want in [("input_space", "scene-linear"), ("output_space", "scene-linear")]:
        check(caps.get(key) == want, f"@{key} = '{caps.get(key)}'")

    # 参数契约
    params = parsed["params"]
    check(len(params) == len(EXPECTED_PARAMS),
          f"参数数量 = {len(params)}（期望 {len(EXPECTED_PARAMS)}）")
    for (ptype, var, label, dflt, pmin, pmax), p in zip(EXPECTED_PARAMS, params):
        ok = (p["type"] == ptype and p["var_name"] == var and p["label"] == label
              and abs(p["default"] - dflt) < 1e-9 and abs(p["min"] - pmin) < 1e-9
              and abs(p["max"] - pmax) < 1e-9
              and p["min"] <= p["default"] <= p["max"])
        check(ok, f"@param {ptype} {var} \"{label}\" {dflt} {pmin} {pmax}"
              + ("" if ok else f" -> 实际 {p}"))

    # 着色器代码 + 红线
    shader_code = parsed["shader_code"]
    check(len(shader_code.strip()) > 400, f"着色器代码非占位档（{len(shader_code.strip())} 字符 > 400）")
    check_shader_redlines(shader_code, content)

    # i18n 片段
    check_i18n()

    report()
    return 0 if all(lv != "FAIL" for lv, _ in results) else 1


def report() -> None:
    print(f"validate_Prism_Aberration — {PLUGIN_PATH.relative_to(REPO_ROOT)}")
    print("-" * 78)
    passed = sum(1 for lv, _ in results if lv == "PASS")
    for lv, msg in results:
        print(f"[{lv}] {msg}")
    print("-" * 78)
    print(f"结果: {passed}/{len(results)} 通过, {len(results) - passed} 失败/警告")
    print("退出码: " + ("0 (ALL PASS)" if all(lv != "FAIL" for lv, _ in results) else "1 (HAS FAILURES)"))


if __name__ == "__main__":
    sys.exit(main())
