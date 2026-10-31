#!/usr/bin/env python3
"""
validate_Skin_Even_Matte.py - headless 解析验证（计划03）

验证对象：
  plugins/Skin_Even_Matte.tglsl
  docs/plugins/2026-09-28/i18n/Skin_Even_Matte.json
  docs/plugins/2026-09-28/specs/Skin_Even_Matte.md
  docs/plugins/2026-09-28/cards/Skin_Even_Matte.md

验证内容：
  1. 真实解析器复现：优先调用 src/features/color/core/plugin_manager.py 的
     PluginManager._parse_plugin_file（与运行时完全同源）；仓库外/导入失败时
     回退到本文件内嵌的同源正则复刻。
  2. 元数据契约：包裹行、@name/@i18n_name/@author/@version/@description、
     capability manifest、@preview_cost、3 个 slider 参数的类型/默认/范围。
  3. GLSL 红线（STYLE_NOTES §7）：循环上限编译期常量、float 字面量带 .0、
     无未声明 u_* 引用、无 clamp(0,1)、无品牌商标名、花括号配平、
     process 入口/delta 输出/首行早退出；另核验本插件的设计声明
     （固定 17 tap、零 pow、零三角函数、唯一增量 clamp）。
  4. 算法数值验证：Python 双生子实现（算法级镜像 .tglsl 常量与控制流，
     纹理按最近邻 texel 建模），在合成 patch 上断言：
     均匀肤色严格恒等 / 肤色斑点被衰减 / 非肤色严格直通 /
     texture_keep=1.0 恒等 / 典型肤色检出 / 非肤色色度拒绝。
  5. i18n 片段结构与 .tglsl Label 的一致性（zh_CN + en_US）。
  6. 规格/卡片交付物存在性与关键内容（卡片须注明与 bilateral_skin_smooth
     的差异定位）。

用法：python3 docs/plugins/2026-09-28/tools/validate_Skin_Even_Matte.py
退出码：0 = 全部通过；1 = 存在 FAIL。
"""

import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# 路径解析：tools -> 2026-09-28 -> plugins(docs) -> docs -> 仓库根
# ----------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_PATH = REPO_ROOT / "plugins" / "Skin_Even_Matte.tglsl"
I18N_PATH = Path(__file__).resolve().parents[1] / "i18n" / "Skin_Even_Matte.json"
SPEC_PATH = Path(__file__).resolve().parents[1] / "specs" / "Skin_Even_Matte.md"
CARD_PATH = Path(__file__).resolve().parents[1] / "cards" / "Skin_Even_Matte.md"

PLUGIN_ID = "Skin_Even_Matte"
EXPECTED_PARAMS = [
    ("slider", "radius", "Radius", 0.4, 0.0, 1.0),
    ("slider", "skin_threshold", "Skin Threshold", 0.7, 0.0, 1.0),
    ("slider", "texture_keep", "Texture Keep", 0.5, 0.0, 1.0),
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
    check(re.search(r"return\s+color\s*\+\s*\(\s*\w+\s*-\s*original_color\s*\)\s*;", code) is not None,
          "delta 输出模式（return color + (<result> - original_color);）")
    check(re.search(r"vec3\s+process[^{]*\{\s*if\s*\(\s*texture_keep\s*>\s*0\.999\s*\)\s*return\s*color;", code) is not None,
          "首行早退出（texture_keep > 0.999 时频率分离精确重建原值）")

    # 循环上限必须为 const int 声明的编译期常量（红线 #2）
    const_ints = set(re.findall(r"const\s+int\s+(\w+)\s*=", code))
    loop_bounds = re.findall(r"for\s*\(\s*int\s+\w+\s*=[^;]+;\s*\w+\s*<\s*(\w+)\s*;", code)
    check(len(loop_bounds) > 0, "存在 for 循环（常量上限模式）")
    for b in loop_bounds:
        check(b in const_ints, f"循环上限 '{b}' 为编译期 const int 常量")

    # 固定次数采样核（本插件设计声明）：tap 数 = 1 + NUM_RINGS * NUM_RING_TAPS
    m_taps = re.search(r"const\s+int\s+NUM_RING_TAPS\s*=\s*(\d+)\s*;", code)
    m_rings = re.search(r"const\s+int\s+NUM_RINGS\s*=\s*(\d+)\s*;", code)
    if m_taps and m_rings:
        taps = 1 + int(m_rings.group(1)) * int(m_taps.group(1))
        check(taps == 17, f"固定采样核 tap 数 = {taps}（期望 17：中心 + 2 环 x 8 tap）")
    else:
        check(False, "NUM_RING_TAPS / NUM_RINGS 常量声明存在")

    # float 字面量：无小数点的裸整数只允许出现在白名单整数语境
    safe_int_contexts = [
        r"const\s+int\s+\w+\s*=\s*\d+\s*;",          # const int N = 8;
        r"for\s*\(\s*int\s+\w+\s*=\s*\d+\s*;",       # for (int i = 0;
        r"\w+\[\s*\d+\s*\]",                         # 数组尺寸 RING_DIRS[8] / vec2[8](
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

    # clamp 红线（红线 #3）：不允许 clamp(...,0,1)；唯一允许的是带注释理由的
    # ±0.45 效果增量限幅（matte 边界防 halo）
    clamps = re.findall(r"clamp\s*\([^;]+;", code)
    clamp01 = [c for c in clamps if re.search(r"clamp\s*\([^,]+,\s*0\.0\s*,\s*1\.0", c)]
    check(not clamp01, "无 clamp(0,1)（HDR 红线）")
    inc_clamps = [c for c in clamps if re.search(r"clamp\s*\(\s*detail\s*,\s*-0\.45\s*,\s*0\.45\s*\)", c)]
    check(len(clamps) == 1 and len(inc_clamps) == 1,
          f"唯一 clamp 为增量限幅 clamp(detail, -0.45, 0.45)（实际 clamp 数 {len(clamps)}）")

    # 设计声明：零 pow / 零三角函数（原生编码域频率分离）
    check(re.search(r"\bpow\s*\(", code) is None, "全文件零 pow（原生编码域运算，无 gamma 转换）")
    check(re.search(r"\b(?:cos|sin|tan|atan|exp|log|exp2|log2)\s*\(", code) is None,
          "全文件零三角/超越函数（方向表 + const mat2 旋转）")

    # 分母守护（红线 #4）
    check("max(sum_weight, 0.001)" in code, "归一化分母下限守护 max(sum_weight, 0.001)")

    # YCbCr 肤色检测关键常量在场（与 Python 双生子同值）
    for const_name in ("CB_WEIGHTS", "CR_WEIGHTS", "CB_CENTER", "CB_HALF_BASE",
                       "CR_CENTER", "CR_HALF_BASE", "WINDOW_FEATHER"):
        check(re.search(rf"const\s+(?:vec3|float)\s+{const_name}\b", code) is not None,
              f"肤色检测常量 {const_name} 在场")

    # 花括号/圆括号配平（注释已剥离）
    check(code.count("{") == code.count("}"), f"花括号配平（{code.count('{')}/{code.count('}')}）")
    check(code.count("(") == code.count(")"), f"圆括号配平（{code.count('(')}/{code.count(')')}）")

    # 品牌商标名（对全文含注释/元数据检查）
    for pat in BRAND_PATTERNS:
        hits = re.findall(pat, raw_content, flags=re.IGNORECASE)
        check(not hits, f"无品牌商标名 {pat}" + ("" if not hits else f" -> 命中 {hits}"))


# ----------------------------------------------------------------------
# Python 双生子（算法级镜像 .tglsl；纹理按最近邻 texel 建模）
# ----------------------------------------------------------------------
CB_W = (-0.16873589, -0.33126411, 0.5)
CR_W = (0.5, -0.41868759, -0.08131241)
CB_CENTER, CB_HALF_BASE = 0.40000000, 0.09803922
CR_CENTER, CR_HALF_BASE = 0.60000000, 0.07843137
WINDOW_FEATHER = 0.02
RING_DIRS = [
    (1.0, 0.0), (0.70710678, 0.70710678), (0.0, 1.0), (-0.70710678, 0.70710678),
    (-1.0, 0.0), (-0.70710678, -0.70710678), (0.0, -1.0), (0.70710678, -0.70710678),
]
ROT_C, ROT_S = 0.92387953, 0.38268343
NUM_RING_TAPS_T, NUM_RINGS_T = 8, 2


def _smoothstep(e0: float, e1: float, x: float) -> float:
    t = (x - e0) / (e1 - e0)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return t * t * (3.0 - 2.0 * t)


def _chroma_window(x: float, center: float, half: float) -> float:
    lo, hi = center - half, center + half
    return (_smoothstep(lo, lo + WINDOW_FEATHER, x)
            * (1.0 - _smoothstep(hi - WINDOW_FEATHER, hi, x)))


def skin_membership(rgb: Tuple[float, float, float], skin_half: Tuple[float, float]) -> float:
    cb = sum(a * b for a, b in zip(rgb, CB_W)) + 0.5
    cr = sum(a * b for a, b in zip(rgb, CR_W)) + 0.5
    return (_chroma_window(cb, CB_CENTER, skin_half[0])
            * _chroma_window(cr, CR_CENTER, skin_half[1]))


class PatchImage:
    """合成 patch：背景色 + 圆形斑点（最近邻 texel 采样模型）。"""

    def __init__(self, background: Tuple[float, float, float],
                 spot: Optional[Tuple[float, float, float, Tuple[float, float, float]]] = None):
        self.background = background
        self.spot = spot  # (x, y, r, color)

    def sample(self, x: int, y: int) -> Tuple[float, float, float]:
        if self.spot is not None:
            sx, sy, r, col = self.spot
            if (x - sx) ** 2 + (y - sy) ** 2 <= r * r:
                return col
        return self.background


def process_twin(img: PatchImage, cx: float, cy: float, size: float,
                 radius: float, skin_threshold: float, texture_keep: float
                 ) -> Tuple[float, float, float]:
    if texture_keep > 0.999:
        return img.sample(int(math.floor(cx + 0.5)), int(math.floor(cy + 0.5)))

    radius_pix = max(2.0, size * (0.003 + radius * 0.012))
    half_scale = 0.4 + (1.0 - 0.4) * skin_threshold
    skin_half = (max(CB_HALF_BASE * half_scale, 0.01),
                 max(CR_HALF_BASE * half_scale, 0.01))

    original = img.sample(int(math.floor(cx + 0.5)), int(math.floor(cy + 0.5)))
    center_gate = skin_membership(original, skin_half)
    accum = [c * (1.0 + center_gate) for c in original]
    sum_w = 1.0 + center_gate

    for ring in range(NUM_RINGS_T):
        ring_radius = radius_pix * (1.0 + float(ring))
        ring_weight = 1.0 - 0.5 * float(ring)
        for (dx, dy) in RING_DIRS:
            d = (dx, dy)
            if ring == 1:
                d = (ROT_C * dx - ROT_S * dy, ROT_S * dx + ROT_C * dy)
            sx = int(math.floor(cx + d[0] * ring_radius + 0.5))
            sy = int(math.floor(cy + d[1] * ring_radius + 0.5))
            s = img.sample(sx, sy)
            w = ring_weight * skin_membership(s, skin_half)
            accum = [a + c * w for a, c in zip(accum, s)]
            sum_w += w

    low = [a / max(sum_w, 0.001) for a in accum]
    detail = [min(max(o - l, -0.45), 0.45) for o, l in zip(original, low)]
    result = [l + d * texture_keep for l, d in zip(low, detail)]

    matte = skin_membership(original, skin_half)
    t = _smoothstep(0.15, 0.85, matte)
    final = [o + (r - o) * t for o, r in zip(original, result)]
    return (final[0], final[1], final[2])


def _dist(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def check_algorithm_twin() -> None:
    size = 33.0
    skin = (0.914, 0.725, 0.620)          # 典型中亮肤色（sRGB 编码域近似）
    blemish = tuple(c * 0.82 for c in skin)  # 亮度压暗、色度仍在窗口内
    sky = (0.45, 0.65, 0.90)              # 蓝天（Cb 窗外）
    sky_spot = tuple(c * 0.85 for c in sky)
    d_radius, d_thresh = 0.4, 0.7

    # 1. 均匀肤色 patch：低通精确等于自身 -> 严格恒等（不产生整体漂移）
    out = process_twin(PatchImage(skin), 16.5, 16.5, size, d_radius, d_thresh, 0.5)
    check(_dist(out, skin) < 1e-9, f"均匀肤色严格恒等（|delta| = {_dist(out, skin):.2e}）")

    # 2. 肤色斑点：中心暗斑被衰减（|out - skin| < |blemish - skin|）
    img = PatchImage(skin, spot=(16, 16, 1.6, blemish))
    out = process_twin(img, 16.5, 16.5, size, d_radius, d_thresh, 0.5)
    ratio = _dist(out, skin) / _dist(blemish, skin)
    check(ratio < 0.95, f"肤色斑点被衰减（残留比例 = {ratio:.3f} < 0.95）")

    # 3. 非肤色 patch（含斑点）：matte = 0 -> 严格直通
    img_ns = PatchImage(sky, spot=(16, 16, 1.6, sky_spot))
    out_ns = process_twin(img_ns, 16.5, 16.5, size, d_radius, d_thresh, 0.5)
    check(_dist(out_ns, sky_spot) < 1e-9, f"非肤色严格直通（|delta| = {_dist(out_ns, sky_spot):.2e}）")

    # 4. texture_keep = 1.0：频率分离精确重建原值 -> 全画面恒等
    out_k1 = process_twin(img, 16.5, 16.5, size, d_radius, d_thresh, 1.0)
    check(_dist(out_k1, blemish) < 1e-9, f"texture_keep=1.0 恒等（|delta| = {_dist(out_k1, blemish):.2e}）")

    # 5. 肤色检出 / 非肤色拒绝（默认窗口）
    half_scale = 0.4 + 0.6 * d_thresh
    sh = (max(CB_HALF_BASE * half_scale, 0.01), max(CR_HALF_BASE * half_scale, 0.01))
    for name, rgb, want_hi in [
        ("典型中亮肤色", skin, True),
        ("深肤色 (0.55,0.38,0.30)", (0.55, 0.38, 0.30), True),
        ("泛红/腮红 (0.75,0.50,0.45)", (0.75, 0.50, 0.45), True),
        ("蓝天 (0.45,0.65,0.90)", sky, False),
        ("绿植 (0.30,0.50,0.25)", (0.30, 0.50, 0.25), False),
        ("中性灰 (0.50,0.50,0.50)", (0.50, 0.50, 0.50), False),
    ]:
        m = skin_membership(rgb, sh)
        check(m > 0.9 if want_hi else m < 0.05,
              f"肤色窗口判定：{name} -> matte = {m:.4f}（{'检出' if want_hi else '拒绝'}）")


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
    check(parsed["name"] == "Skin Even Matte", f"@name = '{parsed['name']}'")
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

    # 算法数值验证（Python 双生子）
    check_algorithm_twin()

    # i18n 片段
    i18n_ok = False
    frag: Dict[str, Any] = {}
    try:
        frag = json.loads(I18N_PATH.read_text(encoding="utf-8"))
        base = frag.get("zh_CN", {}).get("plugins", {}).get(PLUGIN_ID, {})
        en = frag.get("en_US", {}).get("plugins", {}).get(PLUGIN_ID, {})
        check(base.get("name") == "哑光肤色平滑", f"zh_CN name = '{base.get('name')}'")
        check(en.get("name") == "Skin Even Matte", f"en_US name = '{en.get('name')}'")
        zh_params = base.get("params", {})
        en_params = en.get("params", {})
        zh_want = {"radius": "半径", "skin_threshold": "肤色检测阈值", "texture_keep": "纹理保留"}
        for _, var, label, *_ in EXPECTED_PARAMS:
            check(zh_params.get(var) == zh_want[var], f"zh_CN params.{var} = '{zh_params.get(var)}'")
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
    check("Skin Even Matte" in card and "哑光肤色平滑" in card, "卡片含插件英文名与中文名")
    check("bilateral_skin_smooth" in card, "卡片注明与 bilateral_skin_smooth 的差异定位")

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
