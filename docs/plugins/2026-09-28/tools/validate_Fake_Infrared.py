#!/usr/bin/env python3
"""Headless validation for the Fake_Infrared .tglsl plugin (plan03).

Validates, without any GUI / GL context:
  1. Metadata block parses with the SAME regexes as
     src/features/color/core/plugin_manager.py (name / i18n_name / author /
     version / description / preview_cost / capability keys / @param lines,
     including the select option-table parsing path).
  2. Shader-code extraction works via the `//\\s*-{5,}` wrapper rule
     (everything before the first >=5-dash line is metadata).
  3. STYLE_NOTES red lines: bare-int float literals (with the legitimate
     int-index comparisons of the select dispatch whitelisted), undeclared
     u_* refs, non-const for-loop bounds, clamp audit, trademark scan,
     neutral-modifier fast path, perf budget.
  4. i18n fragment (docs/plugins/2026-09-28/i18n/Fake_Infrared.json) has
     zh_CN + en_US name/params for every declared @param.
  5. Numeric model: a Python mirror of the GLSL math checked for grey
     preservation, pivot anchoring, ramp monotonicity, per-mode channel
     semantics (classic/amber/sky-swap), HDR growth without fold-back,
     negative-value finiteness, glow boundedness and NaN/Inf safety on
     adversarial inputs.

Usage: python3 validate_Fake_Infrared.py   (from anywhere; paths resolved
relative to this file). Exit code 0 = all checks passed.
"""

import json
import math
import random
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
PLUGIN = REPO / "plugins" / "Fake_Infrared.tglsl"
I18N = REPO / "docs" / "plugins" / "2026-09-28" / "i18n" / "Fake_Infrared.json"

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

check(name_m is not None and name_m.group(1).strip() == "Fake Infrared",
      "@name present (Fake Infrared)")
check(i18n_m is not None and i18n_m.group(1).strip() == "Fake_Infrared",
      "@i18n_name == plugin_id (Fake_Infrared)")
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
        # mirror of the plugin_manager select branch
        sel_m = re.match(r"([\d.]+)\s+(.+)", vals)
        if sel_m:
            entry["default"] = int(float(sel_m.group(1)))
            entry["options"] = re.findall(r'"([^"]+)"', sel_m.group(2))
    params.append(entry)

check(len(params) == 3, "3 params parsed", f"{[p['var'] for p in params]}")

sel = next((p for p in params if p["type"] == "select"), None)
check(sel is not None and sel["var"] == "channel_map"
      and sel["label"] == "Channel Map"
      and sel.get("default") == 0
      and sel.get("options") == ["Classic", "Amber", "Sky Swap"],
      "select param channel_map ABI intact (index + 3 options)",
      f"{sel}")

expected_sliders = {
    "glow": ("Glow Strength", 0.45, 0.0, 1.0),
    "contrast": ("Contrast", 0.35, 0.0, 1.0),
}
for var, (label, dflt, lo, hi) in expected_sliders.items():
    p = next((x for x in params if x["var"] == var), None)
    ok = (p is not None and p["type"] == "slider" and p["label"] == label
          and abs(p["default"] - dflt) < 1e-9
          and abs(p["min"] - lo) < 1e-9 and abs(p["max"] - hi) < 1e-9
          and p["min"] < p["max"] and p["min"] <= p["default"] <= p["max"])
    check(ok, f"param {var} ABI intact", str(p))

# shader-code extraction (mirror of _extract_shader_code)
end_m = re.search(r"//\s*-{5,}", content)
check(end_m is not None, "metadata wrapper close line (`-{5,}`) present")
shader_code = content[end_m.end():].strip() if end_m else content
# code-only view: comments stripped, so prose never pollutes token-level lints
code_only = "\n".join(re.sub(r"//.*$", "", ln) for ln in shader_code.splitlines())

# metadata directives must not re-match inside the shader region (the real
# parser scans the WHOLE file, so directive-lookalikes in comments would be
# picked up - STYLE_NOTES: metadata lives only in the header region)
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
# bare-int literal lint: any standalone integer token is a red flag in float
# contexts (GLSL strict typing). Whitelisted int contexts here: the select
# dispatch compares an int uniform against constant indices (`== 0` etc.),
# which STYLE_NOTES red line 1 explicitly allows. Scientific literals
# (1e-6) are removed first so their exponent digits are not counted.
code_no_intctx = re.sub(r"[=!<>]=\s*\d+\b", "", code_only)
bare_ints = re.findall(r"(?<![\w.])\d+(?![\w.])",
                       re.sub(r"\d+[eE][-+]?\d+", "", code_no_intctx))
check(not bare_ints, "no bare int literals in float contexts (select index "
      "comparisons whitelisted)", f"found={bare_ints}")

# undeclared u_* uniforms (P0-COL-01). Engine whitelist: this point op
# declares none, so ANY u_* reference is a violation.
u_refs = sorted(set(re.findall(r"\bu_\w+\b", code_only)))
check(not u_refs, "no u_* uniform references in shader code", f"found={u_refs}")

# loop bounds must be compile-time constants (plugin has no loops at all)
loops = re.findall(r"for\s*\(", code_only)
check(not loops, "no for-loops (pure point op)")

# clamp audit: every clamp() must sit on the weight/blend-key domain, never
# on the color signal path. This plugin uses max/min guards only -> the
# expected clamp() count is exactly 0.
clamp_sites = [ln.strip() for ln in code_only.splitlines() if "clamp(" in ln]
check(len(clamp_sites) == 0,
      "clamp audit: 0 clamp() calls (signal path unclamped; guards are max/min)",
      f"sites={clamp_sites}")

# neutral-modifier fast path (this plugin has no global strength param: the
# channel remap is always on, so the early exit skips glow + S-curve only)
check("if (glow < 0.001 && contrast < 0.001) return remapped;" in shader_code,
      "fast-path early exit when glow and contrast are neutral")

# trademark scan (metadata + code). NOTE: `RED` (camera brand) is matched
# case-sensitively and separately - this plugin legitimately uses the color
# word "red" (red foliage, IR-red tint) in its prose.
banned_ci = re.compile(
    r"\b(ARRI|Sony|DaVinci|Photoshop|Kodak|Fuji[film]*|Panasonic|Nikon|"
    r"Canon|Lightroom|Capture One|Aerochrome)\b", re.IGNORECASE)
banned_cs = re.compile(r"\bRED\b")  # all-caps brand only
hits = banned_ci.findall(content) + banned_cs.findall(content)
check(not hits, "no brand/trademark names", f"found={hits}")

# expensive built-in budget (4K realtime: 8M px * 60fps)
pows = len(re.findall(r"\bpow\s*\(", code_only))
taps = len(re.findall(r"texture\s*\(", code_only))
check(pows <= 3 and taps == 0,
      "perf budget: <=3 pow call sites, 0 texture taps", f"pow={pows}, taps={taps}")

# ------------------------------------------------------------- 3. i18n check
i18n = json.loads(I18N.read_text(encoding="utf-8"))
expected_vars = {"channel_map", "glow", "contrast"}
for locale in ("zh_CN", "en_US"):
    node = i18n.get(locale, {}).get("plugins", {}).get("Fake_Infrared")
    ok = (node is not None and node.get("name")
          and set(node.get("params", {})) >= expected_vars
          and all(node["params"][v] for v in expected_vars)
          and node.get("desc", {}).get("short"))
    check(ok, f"i18n {locale}: name + 3 param labels + desc.short",
          f"name={node.get('name') if node else None}")

# --------------------------------------------------- 4. numeric model mirror
LUMA_W = (0.2126, 0.7152, 0.0722)
PIVOT = 0.18
SLOPE_MAX = 2.0
VEG_BOOST = 1.6
SKY_CUT = 1.2
GLOW_FALLOFF = 6.0
GLOW_MAX = 0.3
GLOW_TINT = (1.0, 0.42, 0.52)


def dot3(a, b):
    return sum(x * y for x, y in zip(a, b))


def signed_pow(x, e):
    return math.copysign(max(abs(x), 1e-6) ** e, x) if x != 0.0 else 0.0


def remap(mode, c, ir):
    if mode == 0:
        return (ir, c[0], c[1])
    if mode == 1:
        return (ir, c[0] + (ir - c[0]) * 0.5, c[1])
    if mode == 2:
        return (c[2], c[1], ir)
    return (ir, c[0], c[1])


def process_model(color, channel_map, glow, contrast):
    lum = dot3(color, LUMA_W)
    veg = max(color[1] - max(color[0], color[2]), 0.0)
    sky = max(color[2] - color[1], 0.0)
    ir = lum + veg * VEG_BOOST - min(sky, max(lum, 0.0)) * SKY_CUT

    mapped = remap(channel_map, color, ir)
    if glow < 0.001 and contrast < 0.001:
        return mapped

    result = mapped
    if contrast > 0.001:
        slope = 1.0 + (SLOPE_MAX - 1.0) * contrast
        result = tuple(PIVOT * signed_pow(c / PIVOT, slope) for c in mapped)
    if glow > 0.001:
        dark_w = 1.0 / (1.0 + max(lum, 0.0) * GLOW_FALLOFF)
        veg_w = veg / (veg + 0.04)
        weight = dark_w * (0.4 + 0.6 * veg_w)
        lift = glow * GLOW_MAX * weight
        result = tuple(c + t * lift for c, t in zip(result, GLOW_TINT))
    return result


DEFAULTS = dict(channel_map=0, glow=0.45, contrast=0.35)
FOLIAGE = (0.02, 0.12, 0.02)   # scene-linear green-dominant foliage
SKYPIX = (0.25, 0.45, 0.90)    # scene-linear blue sky

# fast path: both modifiers neutral -> exactly the pure remap
c = (0.31, 0.42, 0.17)
fast = process_model(c, 0, 0.0005, 0.0005)
lum_c = dot3(c, LUMA_W)
ir_c = lum_c + max(c[1] - max(c[0], c[2]), 0.0) * VEG_BOOST
check(all(abs(a - b) < 1e-12 for a, b in zip(fast, (ir_c, c[0], c[1]))),
      "model: fast path equals pure channel remap")

# grey preservation: grey in -> grey out, every mode, contrast on, glow off
for mode in (0, 1, 2):
    v = 0.30
    out = process_model((v, v, v), mode, 0.0, 0.6)
    spread = max(out) - min(out)
    ok = spread < 1e-12
    if not ok:
        check(False, f"model: grey preserved in mode {mode}", f"out={out}")
        break
else:
    check(True, "model: neutral grey preserved in all 3 channel maps")

# pivot anchor: 18% grey exact at full contrast, glow off
out = process_model((PIVOT,) * 3, 0, 0.0, 1.0)
check(all(abs(o - PIVOT) < 1e-12 for o in out),
      "model: 18% grey pivot anchored (contrast = 1.0, glow off)", f"out={out}")

# grey-ramp monotonicity, glow off (strict), across sub-black..HDR
ramp = [-0.2, -0.05, 0.0, 0.005, 0.02, 0.05, 0.1, 0.18, 0.35, 0.6, 1.0,
        1.5, 2.0, 4.0, 8.0, 20.0]
mono_ok = True
for mode in (0, 1, 2):
    outs = [dot3(process_model((v, v, v), mode, 0.0, 0.35), LUMA_W) for v in ramp]
    if not all(a <= b + 1e-12 for a, b in zip(outs, outs[1:])):
        mono_ok = False
        break
check(mono_ok, "model: grey ramp monotone over [-0.2, 20] (glow off, all modes)")

# with the veiling glare on, monotone above 0.01 (deep-shadow softening is
# the documented, bounded, intentional IR glare behaviour)
outs = [dot3(process_model((v, v, v), **DEFAULTS), LUMA_W)
        for v in ramp if v >= 0.01]
check(all(a <= b + 1e-4 for a, b in zip(outs, outs[1:])),
      "model: defaults monotone on [0.01, 20] (glare veil within tolerance)")

# classic: vegetation turns red-dominant, sky sinks (blue > red)
out = process_model(FOLIAGE, **DEFAULTS)
check(out[0] > out[1] and out[0] > out[2],
      "model: classic maps foliage to red-dominant", f"out={tuple(round(x, 4) for x in out)}")
out = process_model(SKYPIX, **DEFAULTS)
check(out[2] > out[0],
      "model: classic sinks the sky (B > R)", f"out={tuple(round(x, 4) for x in out)}")

# amber: foliage warm orange ordering R > G > B
out = process_model(FOLIAGE, 1, 0.45, 0.35)
check(out[0] > out[1] > out[2],
      "model: amber foliage is orange (R > G > B)", f"out={tuple(round(x, 4) for x in out)}")

# sky swap: sky goes fiery (R > B), foliage goes steel-blue (B > R)
out_sky = process_model(SKYPIX, 2, 0.45, 0.35)
out_fol = process_model(FOLIAGE, 2, 0.45, 0.35)
check(out_sky[0] > out_sky[2] and out_fol[2] > out_fol[0],
      "model: sky swap exchanges R/B semantics",
      f"sky={tuple(round(x, 4) for x in out_sky)}, foliage={tuple(round(x, 4) for x in out_fol)}")

# HDR growth: bright green keeps growing, never clamps / folds back
out = process_model((0.0, 10.0, 0.0), **DEFAULTS)
check(out[0] > 10.0, "model: HDR green foliage grows (R > 10)", f"out.r={out[0]:.2f}")

# sub-black stays finite and does not explode
out = process_model((-0.05, -0.05, -0.05), **DEFAULTS)
check(all(math.isfinite(v) and abs(v) < 1.0 for v in out),
      "model: sub-black finite and bounded", f"out={tuple(round(x, 4) for x in out)}")

# glow bound: the veil never adds more than glow * GLOW_MAX per channel
rng = random.Random(20260928)
max_lift = 0.0
for _ in range(4000):
    s = (rng.uniform(-2.0, 20.0), rng.uniform(-2.0, 20.0), rng.uniform(-2.0, 20.0))
    for mode in (0, 1, 2):
        a = process_model(s, mode, 0.45, 0.35)
        b = process_model(s, mode, 0.0, 0.35)
        max_lift = max(max_lift, max(x - y for x, y in zip(a, b)))
check(max_lift <= 0.45 * GLOW_MAX + 1e-9,
      "model: glow lift bounded by glow * 0.3", f"max_lift={max_lift:.4f}")

# glow veil value at absolute black matches the closed form (documented
# black-point lift of the IR glare: tint * 0.45 * 0.3 * 0.4)
out = process_model((0.0, 0.0, 0.0), **DEFAULTS)
expected_veil = tuple(t * (0.45 * GLOW_MAX * 0.4) for t in GLOW_TINT)
check(all(abs(a - b) < 1e-12 for a, b in zip(out, expected_veil)),
      "model: black-point veil equals closed form", f"out={tuple(round(x, 4) for x in out)}")

# NaN / Inf safety over adversarial + random inputs, all modes x param combos
samples = [(1e-6, -1e-6, 0.0), (-8.0, 3.0, -12.0), (1e4, -1e3, 1.0),
           (0.0, 50.0, 0.0), (-1.0, 0.0, 0.5), (0.0, 0.0, 0.0)]
samples += [(rng.uniform(-2.0, 20.0),) * 3 for _ in range(2000)]
samples += [(rng.uniform(-2.0, 20.0), rng.uniform(-2.0, 20.0),
             rng.uniform(-2.0, 20.0)) for _ in range(3000)]
bad = 0
for s in samples:
    for mode in (0, 1, 2):
        for g in (0.0, 0.45, 1.0):
            for ct in (0.0, 0.35, 1.0):
                o = process_model(s, mode, g, ct)
                if not all(math.isfinite(v) for v in o):
                    bad += 1
check(bad == 0, "model: no NaN/Inf over 80k+ evaluations", f"bad={bad}")

# mode dispatch: the three mappings disagree on a colored sample
outs = [process_model((0.3, 0.5, 0.2), 2, 0.45, 0.35) for _ in range(1)]
distinct = (process_model((0.3, 0.5, 0.2), 0, 0.45, 0.35)
            != process_model((0.3, 0.5, 0.2), 1, 0.45, 0.35)
            != process_model((0.3, 0.5, 0.2), 2, 0.45, 0.35))
check(distinct, "model: 3 channel-map modes produce distinct outputs")

# --------------------------------------------------------------- summary
print()
if failures:
    print(f"RESULT: FAIL ({len(failures)} failed: {failures})")
    sys.exit(1)
print("RESULT: PASS - Fake_Infrared.tglsl satisfies spec + STYLE_NOTES red lines")
