#!/usr/bin/env python3
"""Headless validation for the Cross_Process .tglsl plugin (plan03).

Validates, without any GUI / GL context:
  1. Metadata block parses with the SAME regexes as
     src/features/color/core/plugin_manager.py (name / i18n_name / author /
     version / description / preview_cost / capability keys / @param lines,
     including the select option table).
  2. Shader-code extraction works via the `//\\s*-{5,}` wrapper rule
     (everything before the first >=5-dash line is metadata).
  3. STYLE_NOTES red lines: bare-int float literals (select-index compares
     are the sanctioned integer context), undeclared u_* refs, for-loop
     bounds, clamp audit, trademark scan, early exit, perf budget.
  4. Preset coherence: select options <-> preset functions <-> dispatch
     branches are same-count and same-order; the per-preset curve
     coefficients and dye-fog weights are EXTRACTED from the .tglsl and
     must satisfy the monotonicity certificate a*v <= 9*m (a, m, v > 0).
  5. i18n fragment (docs/plugins/2026-09-28/i18n/Cross_Process.json) has
     zh_CN + en_US name/params for every declared @param.
  6. Numeric model: a Python mirror of the GLSL math, driven by the
     extracted coefficients: identity at strength=0, black preservation,
     per-channel monotonicity across [-2, 20] scene-linear, preset hue
     signatures at grey/white, G->R leak visibility, HDR pass-through
     growth, negative-value preservation and NaN/Inf safety.

Usage: python3 validate_Cross_Process.py   (from anywhere; paths resolved
relative to this file). Exit code 0 = all checks passed.
"""

import json
import math
import random
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
PLUGIN = REPO / "plugins" / "Cross_Process.tglsl"
I18N = REPO / "docs" / "plugins" / "2026-09-28" / "i18n" / "Cross_Process.json"

PASS, FAIL = "[PASS]", "[FAIL]"
failures = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    line = f"{PASS if ok else FAIL} {label}"
    if detail:
        line += f" -- {detail}"
    print(line)
    if not ok:
        failures.append(label)
    return ok


# ---------------------------------------------------------------- 1. parse
content = PLUGIN.read_text(encoding="utf-8")

name_m = re.search(r"//\s*@name\s+(.+)", content)
i18n_m = re.search(r"//\s*@i18n_name\s+(.+)", content)
author_m = re.search(r"//\s*@author\s+(.+)", content)
version_m = re.search(r"//\s*@version\s+(.+)", content)
desc_m = re.search(r"//\s*@description\s+(.+)", content)
cost_m = re.search(r"//\s*@preview_cost\s+(high|medium|low|skip)", content)

check(name_m is not None, "@name present")
check(i18n_m is not None and i18n_m.group(1).strip() == "Cross_Process",
      "@i18n_name == plugin_id (Cross_Process)")
check(author_m is not None and author_m.group(1).strip() == "Topos Team",
      "@author == Topos Team")
check(version_m is not None and version_m.group(1).strip() == "1.0",
      "@version present")
check(desc_m is not None and bool(desc_m.group(1).strip()), "@description present")
check(cost_m is not None and cost_m.group(1) == "low",
      "@preview_cost declared (low)")

CAPABILITY_KEYS = (
    "input_space", "output_space", "alpha", "precision", "determinism",
    "export_support", "threading", "graphics_backends", "compute_backends",
    "occlusion",
)
caps = {}
for key in CAPABILITY_KEYS:
    m = re.search(rf"//\s*@{key}\s+(\S+)", content)
    if m:
        caps[key] = m.group(1).strip()
check(caps.get("input_space") == "scene-linear"
      and caps.get("output_space") == "scene-linear",
      "color plugin declares scene-linear in/out",
      f"caps={caps}")

# @param lines, using the exact parser pattern
param_pattern = r"//\s*@param\s+(\w+)\s+(\w+)\s+\"([^\"]+)\"\s+(.+)"
params = []
for m in re.finditer(param_pattern, content):
    ptype, var, label, vals = m.group(1), m.group(2), m.group(3), m.group(4).strip()
    entry = {"type": ptype, "var": var, "label": label}
    if ptype == "slider":
        parts = vals.split()
        entry.update(default=float(parts[0]), min=float(parts[1]), max=float(parts[2]))
    elif ptype == "select":
        dm = re.match(r"([\d.]+)\s+(.+)", vals)
        entry.update(default=int(float(dm.group(1))),
                     options=re.findall(r'"([^"]+)"', dm.group(2)))
    params.append(entry)

check(len(params) == 2, "2 params parsed (select + slider)",
      f"{[p['var'] for p in params]}")

sel = next((p for p in params if p["type"] == "select"), None)
check(sel is not None and sel.get("var") == "preset" and sel.get("label") == "Preset",
      "param preset ABI intact (select)", str(sel))
check(sel is not None and sel.get("options") == ["Classic", "Green Shift", "Warm"]
      and sel.get("default") == 0,
      "preset options + default index",
      f"options={sel.get('options') if sel else None}, default={sel.get('default') if sel else None}")

sld = next((p for p in params if p["type"] == "slider"), None)
ok = (sld is not None and sld.get("var") == "strength"
      and sld.get("label") == "Strength"
      and abs(sld.get("default", -1) - 0.7) < 1e-9
      and abs(sld.get("min", -1) - 0.0) < 1e-9
      and abs(sld.get("max", -1) - 1.0) < 1e-9)
check(ok, "param strength ABI intact (slider)", str(sld))

# shader-code extraction (mirror of _extract_shader_code)
end_m = re.search(r"//\s*-{5,}", content)
check(end_m is not None, "metadata wrapper close line (`-{5,}`) present")
shader_code = content[end_m.end():].strip() if end_m else content
# code-only view: comments stripped, so prose never pollutes token lints
code_only = "\n".join(re.sub(r"//.*$", "", ln) for ln in shader_code.splitlines())

# metadata directives must not re-match inside the shader region
leak = (re.search(r"//\s*@name\s+(.+)", shader_code)
        or re.search(r"//\s*@i18n_name\s+(.+)", shader_code)
        or re.search(r"//\s*@version\s+(.+)", shader_code)
        or re.search(r"//\s*@description\s+(.+)", shader_code)
        or re.search(param_pattern, shader_code))
check(leak is None, "no metadata directives leak into shader code",
      f"leak={leak.group(0) if leak else None}")
check("vec3 process(vec3 color, vec2 uv)" in shader_code,
      "process() entry point present")

# --------------------------------------------------- 2. STYLE_NOTES red lines
# bare-int literal lint. Sanctioned integer contexts per STYLE_NOTES red
# line #1 / Radial_Blur_Pro precedent: select-index comparisons. Those are
# removed before scanning; everything else must carry a decimal point.
code_no_selidx = re.sub(r"preset\s*==\s*\d+", "preset == SELIDX", code_only)
bare_ints = re.findall(r"(?<![\w.])\d+(?![\w.])",
                       re.sub(r"\d+[eE][-+]?\d+", "", code_no_selidx))
check(not bare_ints, "no bare int literals outside select-index compares",
      f"found={bare_ints}")

# undeclared u_* uniforms (P0-COL-01). Engine whitelist only.
u_refs = sorted(set(re.findall(r"\bu_\w+\b", code_only)))
check(not u_refs, "no u_* uniform references in shader code", f"found={u_refs}")

# loop bounds must be compile-time constants (plugin has no loops at all)
loops = re.findall(r"for\s*\(", code_only)
check(not loops, "no for-loops (pure point op)")

# clamp audit: the rational curve family is globally monotone with a
# strictly positive denominator, so NO clamp is needed anywhere - the
# signal path is fully unclamped, HDR included.
clamp_sites = [ln.strip() for ln in code_only.splitlines() if "clamp(" in ln]
check(len(clamp_sites) == 0,
      "clamp audit: zero clamps anywhere (monotone extrapolating curves)",
      f"sites={clamp_sites}")

# early-exit guard on first statement of process
check("if (strength < 0.001) return color;" in shader_code,
      "first-line early exit at strength ~ 0")

# trademark scan (metadata + code)
banned = re.compile(
    r"\b(ARRI|Sony|RED|DaVinci|Photoshop|Kodak|Fuji[film]*|Panasonic|Nikon|"
    r"Canon|Lightroom|Capture One)\b", re.IGNORECASE)
hits = banned.findall(content)
check(not hits, "no brand/trademark names", f"found={hits}")

# expensive built-in budget (4K realtime: 8M px * 60fps). Rational curves
# are mul/add/div only; the single pow is the perceptual strength shaping.
pows = len(re.findall(r"\bpow\s*\(", code_only))
taps = len(re.findall(r"texture\s*\(", code_only))
check(pows == 1 and taps == 0,
      "perf budget: exactly 1 pow (strength curve), 0 texture taps",
      f"pow={pows}, taps={taps}")

# --------------------------------------------- 3. preset coherence + extraction
# dispatch branches: select index 0/1 plus final else fallback (= option 2)
has_b0 = "if (preset == 0)" in code_only and "cross_classic(color);" in code_only
has_b1 = "else if (preset == 1)" in code_only and "cross_green_shift(color);" in code_only
has_else = "cross_warm(color);" in code_only
check(has_b0 and has_b1 and has_else,
      "select options <-> dispatch branches (0/1/else) same count + order")

# extract per-preset curve coefficients and fog weights FROM the file so
# the numeric model below cannot drift from the implementation
FUNC_RE = re.compile(r"vec3 (cross_\w+)\(vec3 c\) \{([^}]*)\}")
CURVE_RE = re.compile(
    r"cp_curve\(c\.([xyz]),\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+)\)")
FOG_RE = re.compile(r"dot\(curved, vec3\(([^)]*)\)\)")

PRESET_ORDER = ["cross_classic", "cross_green_shift", "cross_warm"]
presets = {}
for fname, body in FUNC_RE.findall(shader_code):
    curves = CURVE_RE.findall(body)
    fogs = FOG_RE.findall(body)
    coefs, ok_func = {}, len(curves) == 3 and len(fogs) == 3
    if ok_func:
        for ch, a, m, v in curves:
            coefs[ch] = (float(a), float(m), float(v))
        leak_m = []
        for f in fogs:
            vals = [float(t) for t in re.findall(r"-?\d+\.\d+", f)]
            leak_m.append(vals if len(vals) == 3 else None)
        ok_func = all(x is not None for x in leak_m)
    presets[fname] = {"curves": coefs, "fog": leak_m if ok_func else None,
                      "ok": ok_func and [c[0] for c in curves] == ["x", "y", "z"]}

check(sorted(presets) == sorted(PRESET_ORDER)
      and all(presets[n]["ok"] for n in PRESET_ORDER),
      "3 preset functions parsed with 3 curves + 3 fog vectors each",
      f"found={ {k: presets[k]['ok'] for k in presets} }")

option_to_func = [o.lower().replace(" ", "_") for o in sel.get("options", [])]
check(all(o in n for o, n in zip(option_to_func, PRESET_ORDER)),
      "select options map onto preset functions in order",
      f"{list(zip(sel.get('options', []), PRESET_ORDER))}")

# monotonicity certificate: a + (3m - a*v)*y + m*v*y^2 >= 0 for all y >= 0
# iff a, m, v > 0 and a*v <= 9*m  (derivation in the spec, section 2.2)
cert_ok, cert_detail = True, []
for n in PRESET_ORDER:
    for ch in "xyz":
        a, m, v = presets[n]["curves"][ch]
        margin = 9.0 * m - a * v
        cert_ok &= (a > 0.0 and m > 0.0 and v > 0.0 and margin >= 0.0)
        cert_detail.append(f"{n[6:]}.{ch} av={a * v:.3f} 9m={9.0 * m:.1f}")
check(cert_ok, "monotonicity certificate a*v <= 9*m holds for all 9 curves",
      "; ".join(cert_detail))

# fog weights: small, non-negative, zero diagonal (channel never fogs itself)
fog_ok, fog_max = True, 0.0
for n in PRESET_ORDER:
    for i, row in enumerate(presets[n]["fog"]):
        fog_ok &= row[i] == 0.0 and all(w >= 0.0 and w <= 0.15 for w in row)
        fog_max = max(fog_max, max(row))
check(fog_ok, "fog weights in [0, 0.15], zero diagonal",
      f"max_weight={fog_max}")

# ------------------------------------------------------------- 4. i18n check
i18n = json.loads(I18N.read_text(encoding="utf-8"))
expected_vars = ["preset", "strength"]
for locale in ("zh_CN", "en_US"):
    node = i18n.get(locale, {}).get("plugins", {}).get("Cross_Process")
    ok = node is not None and node.get("name") and all(
        node.get("params", {}).get(v) for v in expected_vars)
    check(ok, f"i18n {locale}: name + 2 param labels",
          f"name={node.get('name') if node else None}")

# --------------------------------------------------- 5. numeric model mirror
def cp_curve(x, a, m, v):
    x2 = x * x
    return x * (a + m * x2) / (1.0 + v * x2)


def process_model(color, preset_func, strength):
    p = presets[preset_func]
    curved = tuple(cp_curve(ci, *p["curves"][ch])
                   for ci, ch in zip(color, "xyz"))
    fog = tuple(sum(curved[j] * p["fog"][i][j] for j in range(3))
                for i in range(3))
    crossed = tuple(c + f for c, f in zip(curved, fog))
    amt = 1.0 - (1.0 - strength) ** 1.5
    return tuple(c0 + (cc - c0) * amt for c0, cc in zip(color, crossed))


# identity at strength = 0, all presets
ok = all(
    all(abs(o - i) < 1e-12 for o, i in
        zip(process_model((0.731, 0.213, 0.042), n, 0.0), (0.731, 0.213, 0.042)))
    for n in PRESET_ORDER)
check(ok, "model: strength=0 is exact identity (all presets)")

# black preservation, all presets
ok = all(all(abs(o) < 1e-12
             for o in process_model((0.0, 0.0, 0.0), n, 1.0))
         for n in PRESET_ORDER)
check(ok, "model: black stays black (all presets)")

# per-channel monotonicity on a grey ramp across SDR + HDR + sub-black
ramp = [-2.0, -0.5, -0.05, 0.0, 0.005, 0.02, 0.05, 0.1, 0.18, 0.35, 0.6,
        1.0, 1.5, 2.0, 4.0, 8.0, 20.0]
mono_ok, mono_detail = True, []
for n in PRESET_ORDER:
    outs = [process_model((v, v, v), n, 1.0) for v in ramp]
    for i, ch in enumerate("RGB"):
        col = [o[i] for o in outs]
        if any(a > b + 1e-9 for a, b in zip(col, col[1:])):
            mono_ok = False
    mono_detail.append(f"{n[6:]}:[{outs[8][0]:.3f},{outs[8][1]:.3f},{outs[8][2]:.3f}]"
                       f"@0.18")
check(mono_ok, "model: per-channel monotone over [-2, 20] ramp (all presets)",
      f"grey@0.18 -> {'; '.join(mono_detail)}")

# preset hue signatures at full strength
sig = {
    "cross_classic":    {0.18: ("B", "G", "R"), 1.0: ("R", "G", "B")},
    "cross_green_shift": {0.18: ("G", None, None), 1.0: ("G", None, None)},
    "cross_warm":       {0.18: ("R", "G", "B"), 1.0: ("R", "G", "B")},
}
sig_ok, sig_detail = True, []
for n, cases in sig.items():
    for x, order in cases.items():
        o = process_model((x, x, x), n, 1.0)
        comp = dict(zip("RGB", o))
        for hi, lo in zip(order, order[1:]):
            if hi and lo:
                sig_ok &= comp[hi] > comp[lo]
        sig_detail.append(f"{n[6:]}@{x}:({o[0]:.3f},{o[1]:.3f},{o[2]:.3f})")
check(sig_ok, "model: preset hue signatures at grey/white",
      "; ".join(sig_detail))

# marquee crosstalk: pure green must leak into R (classic G->R fog)
gr = process_model((0.0, 0.5, 0.0), "cross_classic", 1.0)
check(gr[0] > 0.01,
      "model: G->R leak visible on pure green input",
      f"R_out={gr[0]:.4f} (from G=0.5, R=B=0)")

# HDR growth: 10.0 grey stays far above display white, all channels,
# all presets (no clamp fold-back, no explosion)
hdr_ok, hdr_detail = True, []
for n in PRESET_ORDER:
    o = process_model((10.0, 10.0, 10.0), n, 1.0)
    hdr_ok &= all(5.0 < c < 20.0 and math.isfinite(c) for c in o)
    hdr_detail.append(f"{n[6:]}:({o[0]:.2f},{o[1]:.2f},{o[2]:.2f})")
check(hdr_ok, "model: HDR (10.0) grows unclamped, stays linear-ish",
      "; ".join(hdr_detail))

# negative preservation: sub-black input stays negative (fog deepens only)
neg_ok = True
for n in PRESET_ORDER:
    o = process_model((-0.05, -0.05, -0.05), n, 1.0)
    neg_ok &= all(c < 0.0 for c in o)
check(neg_ok, "model: sub-black stays negative (all presets)")

# NaN / Inf safety over adversarial + random inputs, presets x strengths
rng = random.Random(20260928)
samples = [(1e-6, -1e-6, 0.0), (-8.0, 3.0, -12.0), (1e4, -1e3, 1.0)]
samples += [(0.0, 0.0, 0.0)] * 3
samples += [(rng.uniform(-2.0, 20.0),) * 3 for _ in range(2000)]
samples += [(rng.uniform(-2.0, 20.0), rng.uniform(-2.0, 20.0),
             rng.uniform(-2.0, 20.0)) for _ in range(3000)]
bad = 0
for s in samples:
    for n in PRESET_ORDER:
        for st in (0.0, 0.35, 0.7, 1.0):
            o = process_model(s, n, st)
            if not all(math.isfinite(v) for v in o):
                bad += 1
check(bad == 0, "model: no NaN/Inf over 36k+ evaluations", f"bad={bad}")

# strength blend monotone: s=0.5 output lies between s=0 and s=1 outputs
c = (0.62, 0.41, 0.23)
blend_ok = True
for n in PRESET_ORDER:
    o0 = process_model(c, n, 0.0)
    o5 = process_model(c, n, 0.5)
    o1 = process_model(c, n, 1.0)
    blend_ok &= all(min(a, b) - 1e-9 <= m <= max(a, b) + 1e-9
                    for a, m, b in zip(o0, o5, o1))
check(blend_ok, "model: strength interpolation monotone (all presets)")

# --------------------------------------------------------------- summary
print()
if failures:
    print(f"RESULT: FAIL ({len(failures)} failed: {failures})")
    sys.exit(1)
print("RESULT: PASS - Cross_Process.tglsl satisfies spec + STYLE_NOTES red lines")
