#!/usr/bin/env python3
"""
validate_Soft_Glow_Bloom.py - headless 解析验证（计划03）

验证对象：
  plugins/Soft_Glow_Bloom.tglsl
  docs/plugins/2026-09-28/i18n/Soft_Glow_Bloom.json
  docs/plugins/2026-09-28/specs/Soft_Glow_Bloom.md
  docs/plugins/2026-09-28/cards/Soft_Glow_Bloom.md

验证内容：
  1. 真实解析器复现：优先调用 src/features/color/core/plugin_manager.py 的
     PluginManager._parse_plugin_file（与运行时完全同源）；仓库外/导入失败时
     回退到本文件内嵌的同源正则复刻。
  2. 元数据契约：包裹行、@name/@i18n_name/@author/@version/@description、
     capability manifest、@preview_cost、3 个 slider 参数的类型/默认/范围。
  3. GLSL 红线（STYLE_NOTES §7）：循环上限编译期常量、float 字面量带 .0、
     无未声明 u_* 引用、无 clamp(0,1)、无品牌商标名、花括号配平、
     process 入口/delta 输出/首行早退出。
  4. i18n 片段结构与 .tglsl Label 的一致性（zh_CN + en_US）。
  5. 规格/卡片交付物存在性与关键内容。

用法：python3 docs/plugins/2026-09-28/tools/validate_Soft_Glow_Bloom.py
退出码：0 = 全部通过；1 = 存在 FAIL。
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# 路径解析：tools -> 2026-09-28 -> plugins(docs) -> docs -> 仓库根
# ----------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_PATH = REPO_ROOT / "plugins" / "Soft_Glow_Bloom.tglsl"
I18N_PATH = Path(__file__).resolve().parents[1] / "i18n" / "Soft_Glow_Bloom.json"
SPEC_PATH = Path(__file__).resolve().parents[1] / "specs" / "Soft_Glow_Bloom.md"
CARD_PATH = Path(__file__).resolve().parents[1] / "cards" / "Soft_Glow_Bloom.md"

PLUGIN_ID = "Soft_Glow_Bloom"
EXPECTED_PARAMS = [
    ("slider", "threshold", "Threshold", 0.5, 0.0, 1.0),
    ("slider", "radius", "Glow Radius", 0.5, 0.0, 1.0),
    ("slider", "intensity", "Blend Intensity", 0.5, 0.0, 1.0),
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
        "i18n_name": first(r"//\s*@i18n_name\s+(.+)"),
        "author": first(r"//\s*@author\s+(.+)") or "Unknown",
        "version": first(r"//\s*@version\s+(.+)"),
        "description": first(r"//\s*@description\s+(.+)"),
        "preview_cost": _pc.group(1).strip() if _pc else None,
        "params": params,
        "capabilities": capabilities,
        "shader_code": shader_code,
    }


def real_parse(content: str) -> Tuple[Optional[Dict[str, Any]], str]:
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


def check_shader_redlines(shader_code: str, raw_content: str) -> None:
    code = strip_comments(shader_code)

    # 入口契约 / 核心惯例
    check("vec3 process(vec3 color, vec2 uv)" in code, "process 入口签名存在")
    check("return color + (result - original_color);" in code,
          "delta 输出模式（return color + (result - original_color);）")
    check(re.search(r"vec3\s+process[^{]*\{\s*if\s*\(\s*intensity\s*<\s*0\.001\s*\)\s*return\s*color;", code) is not None,
          "首行早退出（intensity < 0.001）")

    # 循环上限必须为 const int 声明的编译期常量
    const_ints = set(re.findall(r"const\s+int\s+(\w+)\s*=", code))
    loop_bounds = re.findall(r"for\s*\(\s*int\s+\w+\s*=[^;]+;\s*\w+\s*<\s*(\w+)\s*;", code)
    check(len(loop_bounds) > 0, "存在 for 循环（常量上限模式）")
    for b in loop_bounds:
        check(b in const_ints, f"循环上限 '{b}' 为编译期 const int 常量")
    check(any(re.search(r"if\s*\(\s*\w+\s*>=\s*\w+\s*\)\s*break;", code) for _ in [0]),
          "动态半径经 break 收敛（if (i >= n) break;，n 为运行期标量）")

    # float 字面量：无小数点的裸整数只允许出现在白名单整数语境
    safe_int_contexts = [
        r"const\s+int\s+\w+\s*=\s*\d+\s*;",          # const int N = 8;
        r"for\s*\(\s*int\s+\w+\s*=\s*\d+\s*;",       # for (int i = 0;
        r"\w+\s*\[\s*\d+\s*\]",                      # 数组尺寸 RING_DIRS[8] / vec2[8](
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

    # 未声明 u_* 引用（词边界；重命名机制不覆盖 u_ 前缀，P0-COL-01）
    used_u = set(re.findall(r"\bu_\w+\b", code))
    bad_u = used_u - ALLOWED_U_IDENTS
    check(not bad_u, f"无未声明 u_* 引用（实际引用: {sorted(used_u) or '无'}）"
          + ("" if not bad_u else f" -> 非法: {sorted(bad_u)}"))

    # clamp 红线：不允许 clamp(...,0,1)；任何 clamp 都要给出理由（本插件应无）
    clamps = re.findall(r"clamp\s*\([^;]+;", code)
    clamp01 = [c for c in clamps if re.search(r"clamp\s*\([^,]+,\s*0\.0\s*,\s*1\.0", c)]
    check(not clamp01, "无 clamp(0,1)（HDR 红线）")
    check(not clamps, "全文件零 clamp（增量/显示边界均不需要）", warn_only=True)

    # 分母守护
    check("max(sum_weight, 0.001)" in code, "归一化分母下限守护 max(sum_weight, 0.001)")

    # 花括号/圆括号配平（注释已剥离）
    check(code.count("{") == code.count("}"), f"花括号配平（{code.count('{')}/{code.count('}')}）")
    check(code.count("(") == code.count(")"), f"圆括号配平（{code.count('(')}/{code.count(')')}）")

    # 品牌商标名（对全文含注释/元数据检查）
    for pat in BRAND_PATTERNS:
        hits = re.findall(pat, raw_content, flags=re.IGNORECASE)
        check(not hits, f"无品牌商标名 {pat}" + ("" if not hits else f" -> 命中 {hits}"))


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main() -> int:
    content = PLUGIN_PATH.read_text(encoding="utf-8")
    check(content.startswith("// --- Topos Plugin Metadata ---"),
          "头部首行包裹注释 // --- Topos Plugin Metadata ---")
    check(re.search(r"//\s*-{5,}", "\n".join(content.split("\n")[:30])) is not None,
          "头部含闭合包裹行 // -----------------------------（元数据全部位于其前）")

    parsed_real, parser_src = real_parse(content)
    parsed = parsed_real or fallback_parse(content)
    check(True, f"解析器: {parser_src}")

    # 元数据契约（i18n_name 不在解析器返回结构中，从原文提取比对）
    _i18n_m = re.search(r"//\s*@i18n_name\s+(.+)", content)
    i18n_name = _i18n_m.group(1).strip() if _i18n_m else ""
    check(parsed["name"] == "Soft Glow Bloom", f"@name = '{parsed['name']}'")
    check(i18n_name == PLUGIN_ID, f"@i18n_name = '{i18n_name}'（= 文件名 stem）")
    check(parsed["author"] == "Topos Team", f"@author = '{parsed['author']}'")
    check(parsed["version"] == "1.0", f"@version = '{parsed['version']}'")
    check(bool(parsed["description"]), f"@description 非空")
    check(parsed.get("preview_cost") == "medium", f"@preview_cost = '{parsed.get('preview_cost')}'")

    caps = parsed["capabilities"]
    for key, want in [("input_space", "scene-linear"), ("output_space", "scene-linear"),
                      ("alpha", "premultiplied"), ("precision", "float16"),
                      ("determinism", "deterministic"), ("export_support", "video"),
                      ("threading", "gpu"), ("graphics_backends", "opengl"),
                      ("compute_backends", "glsl_fragment")]:
        check(caps.get(key) == want, f"@{key} = '{caps.get(key)}'")

    # 参数契约
    params = parsed["params"]
    check(len(params) == len(EXPECTED_PARAMS), f"参数数量 = {len(params)}（期望 {len(EXPECTED_PARAMS)}）")
    for (ptype, var, label, dflt, pmin, pmax), p in zip(EXPECTED_PARAMS, params):
        ok = (p["type"] == ptype and p["var_name"] == var and p["label"] == label
              and abs(p["default"] - dflt) < 1e-9 and abs(p["min"] - pmin) < 1e-9
              and abs(p["max"] - pmax) < 1e-9)
        check(ok, f"@param {ptype} {var} \"{label}\" {dflt} {pmin} {pmax}"
              + ("" if ok else f" -> 实际 {p}"))

    # 着色器代码 + 红线
    shader_code = parsed["shader_code"]
    check(len(shader_code.strip()) > 400, f"着色器代码非占位档（{len(shader_code.strip())} 字符 > 400）")
    check_shader_redlines(shader_code, content)

    # i18n 片段
    i18n_ok = False
    frag: Dict[str, Any] = {}
    try:
        frag = json.loads(I18N_PATH.read_text(encoding="utf-8"))
        base = frag.get("zh_CN", {}).get("plugins", {}).get(PLUGIN_ID, {})
        en = frag.get("en_US", {}).get("plugins", {}).get(PLUGIN_ID, {})
        check(base.get("name") == "柔光泛光", f"zh_CN name = '{base.get('name')}'")
        check(en.get("name") == "Soft Glow Bloom", f"en_US name = '{en.get('name')}'")
        zh_params = base.get("params", {})
        en_params = en.get("params", {})
        for _, var, label, *_ in EXPECTED_PARAMS:
            check(bool(zh_params.get(var)), f"zh_CN params.{var} = '{zh_params.get(var)}'")
            check(en_params.get(var) == label, f"en_US params.{var} = '{en_params.get(var)}'（= .tglsl 兜底 Label）")
        i18n_ok = True
    except Exception as e:  # noqa: BLE001
        check(False, f"i18n 片段解析失败: {type(e).__name__}: {e}")
    check(i18n_ok and set(frag.keys()) == {"zh_CN", "en_US"}, "i18n 片段仅含 zh_CN + en_US 两个 locale")

    # 规格 / 卡片交付物
    spec = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.exists() else ""
    check(len(spec) > 1000, f"规格文件存在且非平凡（{len(spec)} 字符）")
    for _, var, label, *_ in EXPECTED_PARAMS:
        check(var in spec, f"规格提及参数 {var}")
    card = CARD_PATH.read_text(encoding="utf-8") if CARD_PATH.exists() else ""
    check(len(card) > 500, f"卡片文件存在且非平凡（{len(card)} 字符）")
    check("Soft Glow Bloom" in card and "柔光泛光" in card, "卡片含插件英文名与中文名")

    # 汇总
    fails = [m for lv, m in results if lv == "FAIL"]
    warns = [m for lv, m in results if lv == "WARN"]
    print(f"plugin_id        : {PLUGIN_ID}")
    print(f"plugin_file      : {PLUGIN_PATH.relative_to(REPO_ROOT)}")
    print(f"parser           : {parser_src}")
    print(f"params           : " + ", ".join(p["var_name"] for p in params))
    print(f"shader_chars     : {len(shader_code.strip())}")
    print("-" * 72)
    for lv, msg in results:
        print(f"[{lv:4}] {msg}")
    print("-" * 72)
    print(f"TOTAL: {len(results)} checks | PASS {len(results) - len(fails) - len(warns)} | WARN {len(warns)} | FAIL {len(fails)}")
    print("RESULT: " + ("ALL CHECKS PASSED" if not fails else "VALIDATION FAILED"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
