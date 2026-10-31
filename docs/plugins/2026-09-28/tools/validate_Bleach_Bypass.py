#!/usr/bin/env python3
"""Headless validation for the Bleach_Bypass .tglsl plugin (plan03).

Validates, without any GUI / GL context:
  1. Metadata block parses with the SAME regexes as
     src/features/color/core/plugin_manager.py (name / i18n_name / author /
     version / description / preview_cost / capability keys / @param lines).
  2. Shader-code extraction works via the `//\\s*-{5,}` wrapper rule
     (everything before the first >=5-dash line is metadata).
  3. STYLE_NOTES red lines: bare-int float literals, undeclared u_* refs,
     non-const for-loop bounds, clamp audit, trademark scan, early exit.
  4. i18n fragment (docs/plugins/2026-09-28/i18n/Bleach_Bypass.json) has
     zh_CN + en_US name/params for every declared @param.
  5. Numeric model: a Python mirror of the GLSL math checked for identity
     at strength=0, black preservation, luma monotonicity across
     [-0.2, 20] scene-linear, NaN/Inf safety on adversarial inputs,
     HDR pass-through growth and negative-value preservation.

Usage: python3 validate_Bleach_Bypass.py   (from anywhere; paths resolved
relative to this file). Exit code 0 = all checks passed.
"""

import json
import math
import random
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
PLUGIN = REPO / "plugins" / "Bleach_Bypass.tglsl"
I18N = REPO / "docs" / "plugins" / "2026-09-28" / "i18n" / "Bleach_Bypass.json"

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
check(i18n_m is not None and i18n_m.group(1).strip() == "Bleach_Bypass",
      "@i18n_name == plugin_id (Bleach_Bypass)")
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
    parts = vals.split()
    entry = {"type": ptype, "var": var, "label": label}
    if ptype == "slider":
        entry.update(default=float(parts[0]), min=float(parts[1]), max=float(parts[2]))
    params.append(entry)

sliders = [p for p in params if p["type"] == "slider"]
check(len(sliders) == 3, "3 slider params parsed", f"{[p['var'] for p in sliders]}")
expected_params = {
    "strength": ("Strength", 0.6, 0.0, 1.0),
    "contrast": ("Contrast", 0.4, 0.0, 1.0),
    "saturation": ("Saturation Retention", 0.35, 0.0, 1.0),
}
for var, (label, dflt, lo, hi) in expected_params.items():
    p = next((x for x in sliders if x["var"] == var), None)
    ok = (p is not None and p["label"] == label
          and abs(p["default"] - dflt) < 1e-9
          and abs(p["min"] - lo) < 1e-9 and abs(p["max"] - hi) < 1e-9
          and p["min"] < p["max"] and p["min"] <= p["default"] <= p["max"])
    check(ok, f"param {var} ABI intact", str(p))

# shader-code extraction (mirror of _extract_shader_code)
end_m = re.search(r"//\s*-{5,}", content)
check(end_m is not None, "metadata wrapper close line (`-{5,}`) present")
shader_code = content[end_m.end():].strip() if end_m else content
# code-only view: comments stripped, so prose (e.g. "18% grey") never
# pollutes token-level lints
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
# contexts (GLSL strict typing). Scientific literals (1e-6) are removed
# first so their exponent digits are not counted.
bare_ints = re.findall(r"(?<![\w.])\d+(?![\w.])",
                       re.sub(r"\d+[eE][-+]?\d+", "", code_only))
check(not bare_ints, "no bare int literals in shader code", f"found={bare_ints}")

# undeclared u_* uniforms (P0-COL-01). Engine whitelist only.
u_refs = sorted(set(re.findall(r"\bu_\w+\b", code_only)))
check(not u_refs, "no u_* uniform references in shader code", f"found={u_refs}")

# loop bounds must be compile-time constants (plugin has no loops at all)
loops = re.findall(r"for\s*\(", code_only)
check(not loops, "no for-loops (pure point op)")

# clamp audit: every clamp() must sit on the weight/blend-key domain, never
# on the color signal path. This plugin has exactly one, on `unit`.
clamp_sites = [ln.strip() for ln in code_only.splitlines() if "clamp(" in ln]
ok_clamps = all("float unit" in ln for ln in clamp_sites)
check(len(clamp_sites) == 1 and ok_clamps,
      "clamp audit: single clamp on blend key `unit` only",
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

# expensive built-in budget (4K realtime: 8M px * 60fps)
pows = len(re.findall(r"\bpow\s*\(", code_only))
taps = len(re.findall(r"texture\s*\(", code_only))
check(pows <= 3 and taps == 0,
      "perf budget: <=3 pow calls, 0 texture taps", f"pow={pows}, taps={taps}")

# ------------------------------------------------------------- 3. i18n check
i18n = json.loads(I18N.read_text(encoding="utf-8"))
for locale in ("zh_CN", "en_US"):
    node = i18n.get(locale, {}).get("plugins", {}).get("Bleach_Bypass")
    ok = node is not None and node.get("name") and all(
        node.get("params", {}).get(v) for v in expected_params)
    check(ok, f"i18n {locale}: name + 3 param labels",
          f"name={node.get('name') if node else None}")

# --------------------------------------------------- 4. numeric model mirror
LUMA_W = (0.2126, 0.7152, 0.0722)
PIVOT = 0.18
SLOPE_MAX = 2.0
SILVER_LIFT_MAX = 0.25


def dot3(a, b):
    return sum(x * y for x, y in zip(a, b))


def signed_pow(x, e):
    return math.copysign(max(abs(x), 1e-6) ** e, x) if x != 0.0 else 0.0


def clamp01(x):
    return min(1.0, max(0.0, x))


def process_model(color, strength, contrast, saturation):
    lum = dot3(color, LUMA_W)
    slope = 1.0 + (SLOPE_MAX - 1.0) * contrast
    silver = PIVOT * signed_pow(lum / PIVOT, slope)
    unit = clamp01(lum / (1.0 + max(lum, 0.0)))
    lift = unit * unit * SILVER_LIFT_MAX
    chroma = tuple(c - lum for c in color)
    bypassed = tuple(silver + lift + c * saturation for c in chroma)
    amt = 1.0 - (1.0 - strength) ** 1.5
    return tuple(c0 + (cb - c0) * amt for c0, cb in zip(color, bypassed))


DEFAULTS = dict(strength=0.6, contrast=0.4, saturation=0.35)

# identity at strength = 0
out = process_model((0.731, 0.213, 0.042), 0.0, 0.9, 0.1)
check(all(abs(o - i) < 1e-12 for o, i in zip(out, (0.731, 0.213, 0.042))),
      "model: strength=0 is exact identity")

# black preservation
out = process_model((0.0, 0.0, 0.0), **DEFAULTS)
check(all(abs(o) < 1e-12 for o in out), "model: black stays black")

# luma monotonicity on a grey ramp across SDR + HDR + sub-black
ramp = [-0.2, -0.05, 0.0, 0.005, 0.02, 0.05, 0.1, 0.18, 0.35, 0.6, 1.0,
        1.5, 2.0, 4.0, 8.0, 20.0]
outs = [dot3(process_model((v, v, v), **DEFAULTS), LUMA_W) for v in ramp]
check(all(a <= b + 1e-12 for a, b in zip(outs, outs[1:])),
      "model: output luma monotone over [-0.2, 20] ramp",
      f"outs={['%.4g' % o for o in outs]}")

# pivot anchor: 18% grey luma barely moves at defaults
anchor = dot3(process_model((PIVOT,) * 3, **DEFAULTS), LUMA_W)
check(abs(anchor - PIVOT) < 0.01, "model: 18% grey pivot anchored",
      f"out={anchor:.4f}")

# HDR growth: 10.0 grey must stay well above 1.0 (no clamp fold-back)
hdr_out = dot3(process_model((10.0, 10.0, 10.0), **DEFAULTS), LUMA_W)
check(hdr_out > 10.0, "model: HDR (10.0) grows, never clamped", f"out={hdr_out:.3f}")

# negative preservation: sub-black input stays negative
neg_out = dot3(process_model((-0.05, -0.05, -0.05), **DEFAULTS), LUMA_W)
check(neg_out < 0.0, "model: sub-black stays negative", f"out={neg_out:.4f}")

# NaN / Inf safety over adversarial + random inputs
rng = random.Random(20260928)
bad = 0
samples = [(1e-6, -1e-6, 0.0), (-8.0, 3.0, -12.0), (1e4, -1e3, 1.0)]
samples += [(0.0, 0.0, 0.0)] * 3
samples += [(rng.uniform(-2.0, 20.0),) * 3 for _ in range(2000)]
samples += [(rng.uniform(-2.0, 20.0), rng.uniform(-2.0, 20.0),
             rng.uniform(-2.0, 20.0)) for _ in range(3000)]
for s in samples:
    for ct in (0.0, 0.4, 1.0):
        for st in (0.0, 0.35, 1.0):
            o = process_model(s, 0.6, ct, st)
            if not all(math.isfinite(v) for v in o):
                bad += 1
check(bad == 0, "model: no NaN/Inf over 15k+ evaluations", f"bad={bad}")

# saturation semantics: sat=1 keeps chroma, sat=0 collapses to achromatic
c = (0.9, 0.05, 0.05)
o1 = process_model(c, 1.0, 0.0, 1.0)
spread1 = max(o1) - min(o1)
o0 = process_model(c, 1.0, 0.4, 0.0)
spread0 = max(o0) - min(o0)
check(spread1 > 0.5 and spread0 < 1e-9,
      "model: saturation 1.0 keeps chroma / 0.0 fully silver",
      f"spread1={spread1:.3f}, spread0={spread0:.3g}")

# --------------------------------------------------------------- summary
print()
if failures:
    print(f"RESULT: FAIL ({len(failures)} failed: {failures})")
    sys.exit(1)
print("RESULT: PASS - Bleach_Bypass.tglsl satisfies spec + STYLE_NOTES red lines")
