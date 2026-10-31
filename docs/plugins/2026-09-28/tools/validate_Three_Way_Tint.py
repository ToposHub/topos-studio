#!/usr/bin/env python3
"""Three_Way_Tint.tglsl headless 解析验证（计划03 · 色彩类）.

覆盖 docs/plugins/2026-09-28/specs/Three_Way_Tint.md 第 8 节全部验收标准：
  1) 用真实 PluginManager._parse_plugin_file 解析（导入失败时退回镜像正则）；
  2) 参数 ABI（3 color + 3 slider）类型/默认值/范围逐项比对；
  3) capability 声明（input/output_space、determinism、preview_cost 等）；
  4) 结构红线（包裹行、元数据位置、正文无 @指令 泄漏）；
  5) GLSL 红线（float 字面量 .0、未声明 u_*、clamp(0,1) 审计、商标名、
     零采样/零循环声明核实、process 契约与首行早退出）；
  6) 数学性质（纯 Python 复算参考模型）：分区权重和恒为 1、零强度恒等、
     中性 tint 恒等、HDR 按比例缩放、负值符号保持、权重单调。

用法：
  python3 validate_Three_Way_Tint.py                # 打印报告，全过退出码 0
  python3 validate_Three_Way_Tint.py --update-spec  # 同时刷新规格末尾的验证输出段

不依赖 GUI / OpenGL / 已加载 locale（headless 下 I18N.tr 返回英文兜底文案）。
"""

from __future__ import annotations

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

PLUGIN_PATH = _REPO_ROOT / "plugins" / "Three_Way_Tint.tglsl"
SPEC_PATH = _HERE.parents[1] / "specs" / "Three_Way_Tint.md"
PLUGIN_ID = "Three_Way_Tint"

SPEC_MARK_START = "<!-- Three_Way_Tint:validate start -->"
SPEC_MARK_END = "<!-- Three_Way_Tint:validate end -->"

# 期望值（与规格 §2 / §6 一致）
EXPECTED_NAME = "Three-Way Tint"
EXPECTED_PARAMS: List[Tuple[str, str, Any]] = [
    ("color", "shadow_color", [0.38, 0.46, 0.60]),
    ("slider", "shadow_strength", (0.30, 0.0, 1.0)),
    ("color", "midtone_color", [0.52, 0.50, 0.47]),
    ("slider", "midtone_strength", (0.25, 0.0, 1.0)),
    ("color", "highlight_color", [0.60, 0.53, 0.42]),
    ("slider", "highlight_strength", (0.30, 0.0, 1.0)),
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


def _smoothstep(e0: float, e1: float, x: float) -> float:
    t = min(max((x - e0) / (e1 - e0), 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def _zone_weights(luma: float) -> Tuple[float, float, float]:
    l = max(luma, 0.0)
    t = l / (l + TONE_PIVOT)
    w_s = 1.0 - _smoothstep(0.25, 0.55, t)
    w_h = _smoothstep(0.55, 0.85, t)
    w_m = min(max(1.0 - w_s - w_h, 0.0), 1.0)
    return w_s, w_m, w_h


def _zone_gain(tint: Tuple[float, float, float], w: float, s: float
               ) -> Tuple[float, float, float]:
    amt = w * s
    return tuple(1.0 + (c * 2.0 - 1.0) * amt for c in tint)  # type: ignore[return-value]


def model(color, sc, mc, hc, ss, sm, sh):
    """返回 (输出 RGB, (w_shadow, w_mid, w_high), 色调坐标 t)。"""
    luma = sum(c * w for c, w in zip(color, LUMA_W))
    l = max(luma, 0.0)
    t = l / (l + TONE_PIVOT)
    weights = _zone_weights(luma)
    g1 = _zone_gain(sc, weights[0], ss)
    g2 = _zone_gain(mc, weights[1], sm)
    g3 = _zone_gain(hc, weights[2], sh)
    out = tuple(color[i] * g1[i] * g2[i] * g3[i] for i in range(3))
    return out, weights, t


NEUTRAL = (0.5, 0.5, 0.5)
SHADOW_D = (0.38, 0.46, 0.60)
MID_D = (0.52, 0.50, 0.47)
HIGH_D = (0.60, 0.53, 0.42)


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
    ck.check(bool(parsed.get("description")), "1.5 @description 非空",
             (parsed.get("description") or "")[:60] + "...")
    ck.check(bool(shader_code) and "vec3 process(vec3 color, vec2 uv)" in
             re.sub(r"\s+", " ", shader_code).replace("vec3 process (", "vec3 process("),
             "1.6 shader_code 非空且含 process 契约", f"{len(shader_code)} chars")

    # ---- 2. 参数 ABI ----
    params: List[Dict[str, Any]] = parsed.get("params", [])
    ck.check(len(params) == 6, "2.1 有效参数共 6 个（3 color + 3 slider）",
             f"got {len(params)}")
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
        if isinstance(evals, tuple):  # slider: default/min/max
            ok = ok and tuple(p.get(k) for k in ("default", "min", "max")) == evals
            detail += f" d/m/x={p.get('default')}/{p.get('min')}/{p.get('max')}"
        else:  # color: rgb
            got = [round(v, 6) for v in (p.get("default") or [])]
            ok = ok and got == [round(v, 6) for v in evals]
            detail += f" rgb={got}"
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
    ck.check(len(clamp_lines) == 1 and all("w_midtone" in ln for ln in clamp_lines),
             "5.3 clamp 审计：唯一 clamp 是中间调权重浮点保险",
             "; ".join(clamp_lines[:2]))
    ck.check("clamp" not in shader_code[shader_code.rfind("return"):],
             "5.4 返回路径无 clamp（HDR/负值直通 soft-clip）")

    ck.check("for (" not in code_nc and "for(" not in code_nc,
             "5.5 零循环（无需常量上限检查）")
    ck.check("texture(" not in code_nc and "fast_blur" not in code_nc,
             "5.6 零纹理采样（与头部声明一致，无需 delta 输出模式）")

    ck.check(not TRADEMARK_RE.search(content), "5.7 无品牌商标名")

    m_proc = re.search(r"vec3\s+process\s*\(\s*vec3\s+color\s*,\s*vec2\s+uv\s*\)", code_nc)
    ck.check(m_proc is not None, "5.8 process 契约签名")
    if m_proc:
        head = code_nc[m_proc.end():m_proc.end() + 200]
        ck.check("shadow_strength < 0.001" in head and head.find("if") < head.find("float"),
                 "5.9 首行早退出（三强度全 0 恒等返回）")

    # ---- 6. 数学性质（参考模型复算） ----
    lumas = [x * 0.001 for x in range(-300, 12001)]  # -0.30 .. 12.0
    sums = [sum(_zone_weights(l)) for l in lumas]
    ck.check(all(abs(s - 1.0) < 1e-9 for s in sums),
             "6.1 三区权重和恒为 1（partition of unity）",
             f"max|sum-1|={max(abs(s-1.0) for s in sums):.2e}")

    t_grid = [l / (l + TONE_PIVOT) for l in (max(x, 0.0) for x in lumas)]
    w_s_seq = [_zone_weights(l)[0] for l in lumas]
    w_h_seq = [_zone_weights(l)[2] for l in lumas]
    mono_ok = all(a >= b - 1e-12 for a, b in zip(w_s_seq, w_s_seq[1:])) and \
        all(b >= a - 1e-12 for a, b in zip(w_h_seq, w_h_seq[1:]))
    ck.check(mono_ok, "6.2 阴影权重单调降 / 高光权重单调升（无硬边回摆）")

    samples = [(0.02, 0.02, 0.02), (0.18, 0.18, 0.18), (0.5, 0.5, 0.5),
               (1.0, 1.0, 1.0), (4.0, 4.0, 4.0), (12.0, 12.0, 12.0),
               (-0.5, 0.01, 0.01), (0.9, 0.1, 0.1)]
    ident_zero = all(model(c, SHADOW_D, MID_D, HIGH_D, 0.0, 0.0, 0.0)[0] == c
                     for c in samples)
    ck.check(ident_zero, "6.3 三强度为 0 时严格恒等（含 HDR/负值样点）")

    ident_neutral = all(
        all(abs(a - b) < 1e-12 for a, b in
            zip(model(c, NEUTRAL, NEUTRAL, NEUTRAL, 1.0, 1.0, 1.0)[0], c))
        for c in samples)
    ck.check(ident_neutral, "6.4 tint 全 0.5（中性锚点）时增益恒为 1")

    out_hdr, w_hdr, t_hdr = model((4.0, 4.0, 4.0), NEUTRAL, NEUTRAL, HIGH_D,
                                  0.0, 0.0, 1.0)
    want_hdr = (4.8, 4.24, 3.36)  # 4.0 * (2*0.60, 2*0.53, 2*0.42)
    ck.check(all(abs(a - b) < 1e-9 for a, b in zip(out_hdr, want_hdr)) and
             all(v > 1.0 for v in out_hdr) and abs(w_hdr[2] - 1.0) < 1e-9,
             "6.5 HDR 样点按比例缩放（4.0 -> (4.80,4.24,3.36)，不被压回 [0,1]）",
             f"t={t_hdr:.3f} w_h={w_hdr[2]:.3f}")

    out_neg, w_neg, _ = model((-0.5, 0.01, 0.01), SHADOW_D, NEUTRAL, NEUTRAL,
                              1.0, 0.0, 0.0)
    ck.check(out_neg[0] < 0.0 and abs(w_neg[0] - 1.0) < 1e-9,
             "6.6 负值（out-of-gamut）按纯阴影分区且符号保持",
             f"out.r={out_neg[0]:.4f} w_s={w_neg[0]:.3f}")

    _, w_at_022, _ = model((0.22, 0.22, 0.22), NEUTRAL, NEUTRAL, NEUTRAL, 0, 0, 0)
    _, w_at_006, _ = model((0.06, 0.06, 0.06), NEUTRAL, NEUTRAL, NEUTRAL, 0, 0, 0)
    ck.check(abs(w_at_022[1] - 1.0) < 1e-9 and abs(w_at_006[0] - 1.0) < 1e-9,
             "6.7 锚点：luma 0.22 为纯中间调、0.06 为纯阴影",
             f"0.22 -> (s={w_at_022[0]:.2f}, m={w_at_022[1]:.2f}, h={w_at_022[2]:.2f}); "
             f"0.06 -> s={w_at_006[0]:.2f}")

    return ck, engine


def build_report(ck: Checker, engine: str) -> str:
    passed, total = ck.total_passed()
    lines = [f"engine: {engine}", f"target: plugins/Three_Way_Tint.tglsl",
             f"date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for ok, label, detail in ck.results:
        mark = "OK  " if ok else "FAIL"
        lines.append(f"[{mark}] {label}" + (f" —— {detail}" if detail else ""))
    lines += ["", f"== {passed}/{total} checks passed =="]
    return "\n".join(lines)


def update_spec(report: str) -> None:
    block = (
        f"\n{SPEC_MARK_START}\n## 10. 验证输出"
        f"（tools/validate_Three_Way_Tint.py --update-spec 生成）\n\n```\n"
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
