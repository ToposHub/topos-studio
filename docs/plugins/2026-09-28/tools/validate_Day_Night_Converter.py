#!/usr/bin/env python3
"""Day_Night_Converter.tglsl headless 解析验证（计划03 · 色彩类）.

覆盖 docs/plugins/2026-09-28/specs/Day_Night_Converter.md 第 7 节全部验收标准：
  1) 用真实 PluginManager._parse_plugin_file 解析（导入失败时退回镜像正则）；
  2) 参数 ABI（4 slider，含 exposure 的负最小值）类型/默认值/范围逐项比对；
  3) capability 声明（input/output_space、determinism、preview_cost 等）；
  4) 结构红线（包裹行、元数据位置、正文无 @指令 泄漏）；
  5) GLSL 红线（float 字面量 .0、未声明 u_*、零 clamp 审计、唯一超越函数
     exp2、商标名、零采样/零循环、process 契约与首行早退出）；
  6) 数学性质（纯 Python 复算参考模型）：全零恒等、深度单调加深、保护单调
     提亮、曝光整档精确、冷移方向、满保护天空只剩月光蓝、HDR 按比例
     不压回、负值符号保持、暗区不受保护参数影响；
  7) i18n 片段 JSON 可解析且键覆盖 name + 4 参数 var + desc。

用法：
  python3 validate_Day_Night_Converter.py                # 打印报告，全过退出码 0
  python3 validate_Day_Night_Converter.py --update-spec  # 同时刷新规格末尾的验证输出段

不依赖 GUI / OpenGL / 已加载 locale（headless 下 I18N.tr 返回英文兜底文案）。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 路径定位（容忍仓库路径含空格）
# --------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[4]  # tools -> 2026-09-28 -> plugins -> docs -> repo
if not (_REPO_ROOT / "plugins").is_dir():  # 兜底：向上找 AGENTS.md
    for _p in _HERE.parents:
        if (_p / "AGENTS.md").is_file() and (_p / "plugins").is_dir():
            _REPO_ROOT = _p
            break

PLUGIN_PATH = _REPO_ROOT / "plugins" / "Day_Night_Converter.tglsl"
SPEC_PATH = _HERE.parents[1] / "specs" / "Day_Night_Converter.md"
I18N_PATH = _HERE.parents[1] / "i18n" / "Day_Night_Converter.json"
PLUGIN_ID = "Day_Night_Converter"

SPEC_MARK_START = "<!-- Day_Night_Converter:validate start -->"
SPEC_MARK_END = "<!-- Day_Night_Converter:validate end -->"

# 期望值（与规格 §2 / §5 一致）
EXPECTED_NAME = "Day-Night Converter"
EXPECTED_PARAMS: List[Tuple[str, str, Any]] = [
    ("slider", "night_depth", (0.55, 0.0, 1.0)),
    ("slider", "temperature_shift", (0.45, 0.0, 1.0)),
    ("slider", "sky_protection", (0.50, 0.0, 1.0)),
    ("slider", "exposure", (0.0, -3.0, 3.0)),
]
EXPECTED_CAPS = {
    "input_space": "scene-linear",
    "output_space": "scene-linear",
    "determinism": "deterministic",
    "export_support": "opengl",
    # preview_cost 是顶层字段而非 capabilities 键
}
EXPECTED_PREVIEW_COST = "low"

ALLOWED_BUILTIN_U = {"u_input_tex", "u_tex_size", "u_aspect_ratio"}
TRADEMARK_RE = re.compile(
    r"\b(ARRI|Sony|DaVinci|Photoshop|Panasonic|Canon|Fujifilm|Nikon|Apple|"
    r"Blackmagic|BMD|RED)\b"
)


class Checker:
    """极简检查收集器：[OK]/[FAIL] + 明细。"""

    def __init__(self) -> None:
        self.results: List[Tuple[bool, str, str]] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.results.append((bool(ok), label, detail))
        return bool(ok)

    def total_passed(self) -> Tuple[int, int]:
        return sum(1 for ok, _, _ in self.results if ok), len(self.results)


# --------------------------------------------------------------------------
# 解析引擎：优先真实 PluginManager，失败退回镜像正则
# --------------------------------------------------------------------------
def parse_with_real_manager(path: Path) -> Optional[Dict[str, Any]]:
    try:
        sys.path.insert(0, str(_REPO_ROOT))
        from src.features.color.core.plugin_manager import PluginManager  # noqa: PLC0415

        pm = PluginManager()
        return pm._parse_plugin_file(str(path), PLUGIN_ID)
    except Exception:  # noqa: BLE001 —— 任何导入/解析故障都退回镜像正则
        return None


CAPABILITY_KEYS = (
    "input_space", "output_space", "alpha", "precision",
    "determinism", "export_support", "threading",
    "graphics_backends", "compute_backends", "occlusion",
)


def parse_with_regex_fallback(content: str) -> Dict[str, Any]:
    """镜像 plugin_manager.py 的解析正则（引擎不可用时保证验证仍可运行）。"""

    def first(pattern: str) -> str:
        m = re.search(pattern, content)
        return m.group(1).strip() if m else ""

    params: List[Dict[str, Any]] = []
    for m in re.finditer(r'//\s*@param\s+(\w+)\s+(\w+)\s+"([^"]+)"\s+(.+)', content):
        ptype, var, label, vals = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        param: Dict[str, Any] = {"type": ptype, "var_name": var, "label": label}
        parts = vals.split()
        if ptype == "slider" and len(parts) >= 3:
            param["default"], param["min"], param["max"] = (
                float(parts[0]), float(parts[1]), float(parts[2]))
        elif ptype == "checkbox":
            param["default"] = int(float(parts[0]))
        elif ptype == "color" and len(parts) >= 3:
            param["default"] = [float(x) for x in parts[:3]]
        params.append(param)

    close = re.search(r"//\s*-{5,}", content)
    shader_code = content[close.end():].strip() if close else content

    caps = {}
    for key in CAPABILITY_KEYS:
        m = re.search(rf"//\s*@{key}\s+(\S+)", content)
        if m:
            caps[key] = m.group(1).strip()

    return {
        "plugin_id": PLUGIN_ID,
        "name": first(r"//\s*@name\s+(.+)") or PLUGIN_ID,
        "i18n_name": first(r"//\s*@i18n_name\s+(.+)"),
        "version": first(r"//\s*@version\s+(.+)"),
        "description": first(r"//\s*@description\s+(.+)"),
        "author": first(r"//\s*@author\s+(.+)"),
        "params": params,
        "shader_code": shader_code,
        "capabilities": caps,
        "preview_cost": (re.search(r"//\s*@preview_cost\s+(high|medium|low|skip)", content)
                         or type("M", (), {"group": lambda self, i: None})()).group(1),
    }


# --------------------------------------------------------------------------
# 参考数学模型（与 .tglsl 实现逐行对应，纯 Python 复算）
# --------------------------------------------------------------------------
LUMA_W = (0.2126, 0.7152, 0.0722)
TONE_PIVOT = 0.18
SKY_RAMP = (0.55, 0.85)
DEPTH_MAX_STOPS = 2.3
COOL_GAIN = (0.78, 0.94, 1.16)
MOON_RAMP = (0.60, 0.90)
MOON_GAIN = (0.94, 0.98, 1.06)


def _smoothstep(e0: float, e1: float, x: float) -> float:
    t = min(max((x - e0) / (e1 - e0), 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def model(color, nd, ts, sp, ex):
    """返回 (输出 RGB, (w_sky, night, cool, moon), 色调坐标 t)。"""
    luma = max(sum(c * w for c, w in zip(color, LUMA_W)), 0.0)
    t = luma / (luma + TONE_PIVOT)
    w_sky = _smoothstep(SKY_RAMP[0], SKY_RAMP[1], t) * sp
    night = nd * (1.0 - w_sky)
    stops = ex - DEPTH_MAX_STOPS * night
    cool = ts * night
    moon = _smoothstep(MOON_RAMP[0], MOON_RAMP[1], t) * nd
    g_exp = 2.0 ** stops
    out = tuple(
        color[i] * g_exp
        * (1.0 + (COOL_GAIN[i] - 1.0) * cool)
        * (1.0 + (MOON_GAIN[i] - 1.0) * moon)
        for i in range(3))
    return out, (w_sky, night, cool, moon), t


# --------------------------------------------------------------------------
# 检查项
# --------------------------------------------------------------------------
def strip_glsl_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    return code


def run_checks() -> Tuple[Checker, str]:
    ck = Checker()
    engine = "regex-fallback（真实解析器不可用）"

    if not PLUGIN_PATH.is_file():
        ck.check(False, "0.1 插件文件存在", str(PLUGIN_PATH))
        return ck, engine
    ck.check(True, "0.1 插件文件存在", str(PLUGIN_PATH))

    content = PLUGIN_PATH.read_text(encoding="utf-8")

    parsed = parse_with_real_manager(PLUGIN_PATH)
    if parsed is not None:
        engine = "real PluginManager（src/features/color/core/plugin_manager.py）"
        ck.check(True, "1.1 真实解析器解析成功", f"plugin_id={parsed['plugin_id']}")
    else:
        parsed = parse_with_regex_fallback(content)
        ck.check(True, "1.1 镜像正则解析成功（真实解析器导入失败，降级）")
    shader_code: str = parsed.get("shader_code") or ""

    # ---- 1. 基础元数据 ----
    ck.check(parsed.get("name") == EXPECTED_NAME, "1.2 @name 解析",
             f"got={parsed.get('name')!r}")
    ck.check(parsed.get("author") == "Topos Team", "1.3 @author", f"got={parsed.get('author')!r}")
    ck.check(parsed.get("version") == "1.0", "1.4 @version", f"got={parsed.get('version')!r}")
    ck.check(bool(parsed.get("description")), "1.5 @description 非空（含启发式说明）",
             (parsed.get("description") or "")[:60] + "...")
    ck.check("heuristic" in (parsed.get("description") or "").lower(),
             "1.6 @description 注明启发式性质")
    ck.check(bool(shader_code) and "vec3 process(vec3 color, vec2 uv)" in
             re.sub(r"\s+", " ", shader_code).replace("vec3 process (", "vec3 process("),
             "1.7 shader_code 非空且含 process 契约", f"{len(shader_code)} chars")

    # ---- 2. 参数 ABI ----
    params: List[Dict[str, Any]] = parsed.get("params", [])
    ck.check(len(params) == 4, "2.1 有效参数共 4 个（全 slider）", f"got {len(params)}")
    raw_param_lines = re.findall(r"//\s*@param\b[^\n]*", content)
    sep_lines = [ln for ln in raw_param_lines if ln.strip() == "// @param separator"]
    ck.check(len(raw_param_lines) - len(sep_lines) == len(params),
             "2.2 无 @param 行被解析器静默丢弃",
             f"raw={len(raw_param_lines)} separator={len(sep_lines)} parsed={len(params)}")
    for i, (etype, evar, evals) in enumerate(EXPECTED_PARAMS):
        if i >= len(params):
            ck.check(False, f"2.{3+i} 参数[{evar}]", "缺失")
            continue
        p = params[i]
        ok = p.get("type") == etype and p.get("var_name") == evar
        detail = f"type={p.get('type')} var={p.get('var_name')}"
        ok = ok and tuple(p.get(k) for k in ("default", "min", "max")) == evals
        detail += f" d/m/x={p.get('default')}/{p.get('min')}/{p.get('max')}"
        ok = ok and bool(p.get("label"))  # Label 兜底文案存在
        ck.check(ok, f"2.{3+i} 参数[{evar}] 类型/默认值/范围", detail)

    # ---- 3. capability 声明 ----
    caps: Dict[str, Any] = parsed.get("capabilities", {}) or {}
    for j, (key, want) in enumerate(EXPECTED_CAPS.items()):
        ck.check(caps.get(key) == want, f"3.{1+j} @{key}",
                 f"got={caps.get(key)!r} want={want!r}")
    ck.check(parsed.get("preview_cost") == EXPECTED_PREVIEW_COST,
             f"3.{1+len(EXPECTED_CAPS)} @preview_cost",
             f"got={parsed.get('preview_cost')!r}")

    # ---- 4. 结构红线 ----
    ck.check(content.lstrip().startswith("// --- Topos Plugin Metadata ---"),
             "4.1 头部包裹起始行", content.splitlines()[0])
    close = re.search(r"//\s*-{5,}", content)
    ck.check(close is not None, "4.2 头部包裹结束行 // ----")
    if close:
        leaked = [m.group(0) for m in re.finditer(r"//\s*@\w+", content)
                  if m.start() >= close.end()]
        ck.check(not leaked, "4.3 元数据指令未泄漏进着色器代码",
                 "; ".join(leaked[:3]) if leaked else "body 干净")
    i18n_name = (re.search(r"//\s*@i18n_name\s+(\S+)", content) or
                 type("M", (), {"group": lambda self, i: ""})()).group(1)
    ck.check(i18n_name == PLUGIN_ID, "4.4 @i18n_name 与文件名 stem 一致", i18n_name)

    # ---- 5. GLSL 红线 ----
    code_nc = strip_glsl_comments(shader_code)  # 无注释代码

    u_refs = sorted(set(re.findall(r"\bu_\w+\b", code_nc)))
    bad_u = [u for u in u_refs if u not in ALLOWED_BUILTIN_U]
    ck.check(not bad_u, "5.1 无未声明 u_* 引用（P0-COL-01）",
             f"refs={u_refs or '无'}")

    no_float = re.sub(r"\d+\.\d+(?:[eE][+-]?\d+)?", "", code_nc)
    no_float = re.sub(r"\.\d+", "", no_float)
    bad_ints: List[str] = []
    for m in re.finditer(r"(?<![\w.])\d+(?![\w.])", no_float):
        line = no_float[:m.start()].count("\n")
        src_line = shader_code.splitlines()[line] if line < len(shader_code.splitlines()) else "?"
        if not re.search(r"\bint\b|textureSize", src_line):  # 整数语境白名单
            bad_ints.append(f"L{line+1}: {src_line.strip()[:50]}")
    ck.check(not bad_ints, "5.2 float 字面量全部带 .0（整数语境除外）",
             "; ".join(bad_ints[:3]) if bad_ints else "无违规")

    clamp_lines = [ln.strip() for ln in code_nc.splitlines() if "clamp" in ln]
    ck.check(not clamp_lines, "5.3 零 clamp 审计（全乘性增益，HDR/负值直通 soft-clip）",
             "; ".join(clamp_lines[:2]) if clamp_lines else "无 clamp")
    ck.check("clamp" not in shader_code[shader_code.rfind("return"):],
             "5.4 返回路径无 clamp")

    trans = re.findall(r"\b(pow|exp|log|sin|cos)\s*\(", code_nc)
    exp2_n = len(re.findall(r"\bexp2\s*\(", code_nc))
    ck.check(not trans and exp2_n == 1,
             "5.5 唯一超越函数为 1 次 exp2（档位换算，单条硬件指令）",
             f"pow/exp/log/sin/cos={len(trans)} exp2={exp2_n}")

    ck.check("for (" not in code_nc and "for(" not in code_nc,
             "5.6 零循环（无需常量上限检查）")
    ck.check("texture(" not in code_nc and "fast_blur" not in code_nc,
             "5.7 零纹理采样（与头部声明一致，无需 delta 输出模式）")

    ck.check(not TRADEMARK_RE.search(content), "5.8 无品牌商标名")

    m_proc = re.search(r"vec3\s+process\s*\(\s*vec3\s+color\s*,\s*vec2\s+uv\s*\)", code_nc)
    ck.check(m_proc is not None, "5.9 process 契约签名")
    if m_proc:
        head = code_nc[m_proc.end():m_proc.end() + 200]
        ck.check("night_depth < 0.001" in head and head.find("if") < head.find("float"),
                 "5.10 首行早退出（深度/冷移全 0 且曝光为 0 时恒等返回）")

    # ---- 6. 数学性质（参考模型复算） ----
    samples = [(0.02, 0.02, 0.02), (0.18, 0.18, 0.18), (0.5, 0.5, 0.5),
               (1.0, 1.0, 1.0), (4.0, 4.0, 4.0), (12.0, 12.0, 12.0),
               (-0.5, 0.01, 0.01), (0.9, 0.1, 0.1)]

    ident_zero = all(
        model(c, 0.0, 0.0, sp, 0.0)[0] == c
        for c in samples for sp in (0.0, 1.0))
    ck.check(ident_zero, "6.1 全零参数（深度/冷移/曝光为 0）严格恒等，且与保护量无关")

    depths = [x * 0.05 for x in range(21)]
    luma_seq = [sum(model((0.18, 0.18, 0.18), d, 0.45, 0.5, 0.0)[0]) / 3.0
                for d in depths]
    ck.check(all(a > b + 1e-12 for a, b in zip(luma_seq, luma_seq[1:])),
             "6.2 night_depth 单调加深暗区（无回摆）",
             f"0.0->{luma_seq[0]:.3f} 1.0->{luma_seq[-1]:.3f}")

    pros = [x * 0.1 for x in range(11)]
    sky_seq = [sum(model((2.0, 2.0, 2.0), 0.55, 0.45, sp, 0.0)[0]) / 3.0
               for sp in pros]
    ck.check(all(b >= a - 1e-12 for a, b in zip(sky_seq, sky_seq[1:])),
             "6.3 sky_protection 单调提亮亮区（luma 2.0 天空样点）",
             f"0.0->{sky_seq[0]:.3f} 1.0->{sky_seq[-1]:.3f}")

    out_m1 = model((0.18, 0.18, 0.18), 0.0, 0.0, 0.0, -1.0)[0]
    out_p1 = model((0.18, 0.18, 0.18), 0.0, 0.0, 0.0, 1.0)[0]
    ck.check(all(abs(a - b * 0.5) < 1e-12 for a, b in zip(out_m1, (0.18,) * 3)) and
             all(abs(a - b * 2.0) < 1e-12 for a, b in zip(out_p1, (0.18,) * 3)),
             "6.4 曝光档位精确（-1 档恰 x0.5，+1 档恰 x2.0）")

    _, st_dark, _ = model((0.18, 0.18, 0.18), 0.55, 0.45, 0.0, 0.0)
    _, st_neut, _ = model((0.18, 0.18, 0.18), 0.55, 0.0, 0.0, 0.0)
    gain_dark = 2.0 ** (0.0 - DEPTH_MAX_STOPS * st_dark[1])
    r_g = 1.0 + (COOL_GAIN[0] - 1.0) * st_dark[2]
    b_g = 1.0 + (COOL_GAIN[2] - 1.0) * st_dark[2]
    ck.check(b_g > 1.0 > r_g and st_neut[2] == 0.0,
             "6.5 冷移方向正确（B 增 R 减），冷移为 0 时增益中性",
             f"cool={st_dark[2]:.3f} R_gain={r_g:.3f} B_gain={b_g:.3f}")

    sky = (2.0, 2.0, 2.0)
    out_full, st_full, t_full = model(sky, 1.0, 1.0, 1.0, 0.0)
    want = tuple(sky[i] * MOON_GAIN[i] for i in range(3))  # night=0 -> 只剩月光蓝
    ck.check(st_full[1] == 0.0 and all(abs(a - b) < 1e-12 for a, b in zip(out_full, want)),
             "6.6 满保护时天空不吃下压与冷移，只剩月光蓝（x(0.94,0.98,1.06)）",
             f"t={t_full:.3f} night={st_full[1]:.3f}")

    out_hdr, _, _ = model((4.0, 4.0, 4.0), 0.55, 0.45, 0.0, 0.0)
    ck.check(all(v > 1.0 for v in out_hdr),
             "6.7 HDR 样点不被压回 [0,1]（4.0 默认参数 -> 全通道 > 1）",
             f"-> ({out_hdr[0]:.2f},{out_hdr[1]:.2f},{out_hdr[2]:.2f})")

    base_c = (0.05, 0.02, 0.01)  # luma 约 0.026 -> t 约 0.125
    dbl_c = tuple(v * 2.0 for v in base_c)  # luma 约 0.051 -> t 约 0.222
    # 两个样点都落在恒定权重区（t < 0.55 天空带起点、t < 0.60 蓝移带起点），
    # 增益只依赖亮度权重坐标，档内输出对输入严格成比例。
    o1, _, t1 = model(base_c, 0.55, 0.45, 0.5, 0.0)
    o2, _, t2 = model(dbl_c, 0.55, 0.45, 0.5, 0.0)
    ck.check(t1 < SKY_RAMP[0] and t2 < SKY_RAMP[0] and
             all(abs(b - a * 2.0) < 1e-12 for a, b in zip(o1, o2)),
             "6.8 全乘性：恒定权重档内输出对输入成比例（x2 输入 -> x2 输出）",
             f"t={t1:.3f}/{t2:.3f}")

    out_neg, _, _ = model((-0.5, 0.01, 0.01), 0.55, 0.45, 0.5, 0.0)
    ck.check(out_neg[0] < 0.0,
             "6.9 负值（out-of-gamut）符号保持", f"out.r={out_neg[0]:.4f}")

    dark_sp0, _, _ = model((0.18, 0.18, 0.18), 0.55, 0.45, 0.0, 0.0)
    dark_sp1, _, _ = model((0.18, 0.18, 0.18), 0.55, 0.45, 1.0, 0.0)
    ck.check(dark_sp0 == dark_sp1,
             "6.10 暗区（t <= 0.55）完全不受 sky_protection 影响")

    # ---- 7. i18n 片段 ----
    if I18N_PATH.is_file():
        try:
            frag = json.loads(I18N_PATH.read_text(encoding="utf-8"))
            ok_i18n = True
            detail_i18n = "zh_CN + en_US"
        except (json.JSONDecodeError, OSError) as exc:
            frag, ok_i18n, detail_i18n = {}, False, f"解析失败: {exc}"
    else:
        frag, ok_i18n, detail_i18n = {}, False, str(I18N_PATH)
    ck.check(ok_i18n, "7.1 i18n 片段存在且 JSON 可解析", detail_i18n)
    var_names = [v for _, v, _ in EXPECTED_PARAMS]
    cover_ok = True
    cover_detail = ""
    for loc in ("zh_CN", "en_US"):
        node = (frag.get(loc) or {}).get("plugins", {}).get(PLUGIN_ID, {})
        missing = []
        if not node.get("name"):
            missing.append("name")
        missing += [v for v in var_names if v not in (node.get("params") or {})]
        if not (node.get("desc") or {}).get("short"):
            missing.append("desc.short")
        if missing:
            cover_ok = False
            cover_detail += f" {loc}: 缺 {missing}"
    ck.check(cover_ok, "7.2 zh_CN/en_US 键覆盖 name + 4 参数 + desc.short",
             cover_detail or "全覆盖")

    return ck, engine


def build_report(ck: Checker, engine: str) -> str:
    passed, total = ck.total_passed()
    lines = [f"engine: {engine}", f"target: plugins/Day_Night_Converter.tglsl",
             f"date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for ok, label, detail in ck.results:
        mark = "OK  " if ok else "FAIL"
        lines.append(f"[{mark}] {label}" + (f" —— {detail}" if detail else ""))
    lines += ["", f"== {passed}/{total} checks passed =="]
    return "\n".join(lines)


def update_spec(report: str) -> None:
    block = (
        f"\n{SPEC_MARK_START}\n## 9. 验证输出"
        f"（tools/validate_Day_Night_Converter.py --update-spec 生成）\n\n```\n"
        f"{report}\n```\n{SPEC_MARK_END}\n"
    )
    text = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.is_file() else ""
    if SPEC_MARK_START in text and SPEC_MARK_END in text:
        head = text.split(SPEC_MARK_START)[0]
        tail = text.split(SPEC_MARK_END)[-1]
        text = (head.rstrip("\n") + "\n" + block.strip("\n") + "\n" + tail.lstrip("\n"))
    else:
        text = text.rstrip("\n") + "\n" + block
    SPEC_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    ck, engine = run_checks()
    report = build_report(ck, engine)
    print(report)
    if "--update-spec" in sys.argv:
        update_spec(report)
        print(f"\n[spec updated] {SPEC_PATH}")
    passed, total = ck.total_passed()
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
