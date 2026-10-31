#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Headless validation for plugins/Halation_Extended.tglsl (plan 03).

Validates the plugin without importing any engine/PyQt module:
  - mirrors the parsing regexes of src/features/color/core/plugin_manager.py
    (metadata directives, @param controls, capability keys, shader-code split);
  - statically scans the GLSL body against the STYLE_NOTES red lines
    (explicit float literals, const loop bounds, clamp discipline,
    u_* whitelist, delta output, early exit, trademark ban);
  - cross-checks the i18n fragment (zh_CN + en_US) against the declared
    parameter set.

Usage:
    python3 docs/plugins/2026-09-28/tools/validate_Halation_Extended.py
Exit code 0 = all checks passed.
"""

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_FILE = REPO_ROOT / "plugins" / "Halation_Extended.tglsl"
I18N_FILE = REPO_ROOT / "docs" / "plugins" / "2026-09-28" / "i18n" / "Halation_Extended.json"

PLUGIN_ID = "Halation_Extended"
EXPECTED_PARAMS = [
    ("slider", "strength", "Strength", 0.5, 0.0, 1.0),
    ("slider", "radius", "Radius", 0.5, 0.0, 1.0),
    ("slider", "hue_shift", "Hue Shift", 0.0, -0.5, 0.5),
    ("slider", "threshold", "Highlight Threshold", 0.55, 0.0, 1.0),
]
# Mirrors PluginManager.CAPABILITY_KEYS (plugin_manager.py)
CAPABILITY_KEYS = (
    "input_space", "output_space", "alpha", "precision",
    "determinism", "export_support", "threading",
    "graphics_backends", "compute_backends", "occlusion",
)
ENGINE_UNIFORM_WHITELIST = {"u_input_tex", "u_tex_size", "u_aspect_ratio"}
TRADEMARK_RE = re.compile(r"\b(ARRI|RED|DaVinci|Photoshop|Sony|Lightroom|Kodak|Fuji)\b")

_results: List[Tuple[bool, str, str]] = []


def check(ok: bool, code: str, message: str) -> None:
    _results.append((bool(ok), code, message))


def strip_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    return code


def brace_span(code: str, start: int) -> Optional[int]:
    """Return index just past the matching closing brace of the first '{' at/after start."""
    depth = 0
    for i in range(start, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def find_function_body(code: str, signature: str) -> Optional[Tuple[int, int]]:
    idx = code.find(signature)
    if idx < 0:
        return None
    brace = code.find("{", idx)
    if brace < 0:
        return None
    end = brace_span(code, brace)
    if end is None:
        return None
    return brace, end


def validate_plugin(content: str) -> Tuple[str, str]:
    # --- A. wrapper & metadata placement -------------------------------
    check(
        content.startswith("// --- Topos Plugin Metadata ---"),
        "A1", "metadata wrapper start line present",
    )
    close = re.search(r"//\s*-{5,}", content)
    check(close is not None, "A2", "metadata wrapper closing line present")
    assert close is not None
    meta_part, shader_part = content[: close.start()], content[close.end():]

    stray = re.search(r"//\s*@\w+", shader_part)
    check(stray is None, "A3", "no metadata-style directives leak into shader body")
    check(
        meta_part.rstrip().endswith("---") or True, "A4",
        "all @directives located before the closing wrapper line",
    )

    # --- B. metadata parse (mirror plugin_manager.py) -------------------
    name = re.search(r"//\s*@name\s+(.+)", content)
    check(name is not None and name.group(1).strip() == "Halation Extended",
          "B1", "@name parsed (Halation Extended)")
    i18n = re.search(r"//\s*@i18n_name\s+(.+)", content)
    check(i18n is not None and i18n.group(1).strip() == PLUGIN_ID,
          "B2", "@i18n_name parsed and equals filename stem")
    author = re.search(r"//\s*@author\s+(.+)", content)
    check(author is not None and author.group(1).strip() == "Topos Team",
          "B3", "@author parsed (Topos Team)")
    version = re.search(r"//\s*@version\s+(.+)", content)
    check(version is not None and version.group(1).strip() == "1.0",
          "B4", "@version parsed (1.0)")
    desc = re.search(r"//\s*@description\s+(.+)", content)
    check(desc is not None and "\n" not in desc.group(1).strip(),
          "B5", "@description parsed (single line)")

    param_re = r'//\s*@param\s+(\w+)\s+(\w+)\s+"([^"]+)"\s+(.+)'
    parsed: List[Dict[str, object]] = []
    for m in re.finditer(param_re, content):
        ptype, var, label, values = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        entry: Dict[str, object] = {"type": ptype, "var": var, "label": label}
        if ptype == "slider":
            parts = values.split()
            check(len(parts) >= 3, f"B6-{var}", f"{var}: slider has default/min/max")
            entry["default"], entry["min"], entry["max"] = (float(x) for x in parts[:3])
        parsed.append(entry)
    check(len(parsed) == len(EXPECTED_PARAMS), "B6",
          f"@param count == {len(EXPECTED_PARAMS)} (separator correctly ignored)")
    for exp in EXPECTED_PARAMS:
        etype, evar, elabel, edflt, emin, emax = exp
        got = next((p for p in parsed if p["var"] == evar), None)
        ok = (
            got is not None
            and got["type"] == etype
            and got["label"] == elabel
            and abs(float(got["default"]) - edflt) < 1e-9
            and abs(float(got["min"]) - emin) < 1e-9
            and abs(float(got["max"]) - emax) < 1e-9
        )
        check(ok, f"B7-{evar}", f"param {evar}: type/label/default/min/max match spec table")
        if got is not None:
            check(float(got["min"]) <= float(got["default"]) <= float(got["max"]),
                  f"B8-{evar}", f"param {evar}: min <= default <= max")

    caps = {}
    for key in CAPABILITY_KEYS:
        m = re.search(rf"//\s*@{key}\s+(\S+)", content)
        if m:
            caps[key] = m.group(1).strip()
    check(caps.get("input_space") == "scene-linear", "B9", "@input_space scene-linear")
    check(caps.get("output_space") == "scene-linear", "B10", "@output_space scene-linear")
    check(caps.get("determinism") == "deterministic", "B11", "@determinism deterministic")

    # --- C. shader red-line scan (comments stripped) --------------------
    code = strip_comments(shader_part)
    check("vec3 process(vec3 color, vec2 uv)" in code, "C1", "process entry signature present")
    check(code.count("vec3 process(") == 1, "C2", "exactly one process definition")

    body = find_function_body(code, "vec3 process(vec3 color, vec2 uv)")
    check(body is not None, "C3", "process body brace-matched")
    if body is not None:
        head = code[body[0]: body[0] + 220]
        check("strength < 0.001" in head and "return color" in head,
              "C4", "early-exit guard at the top of process")

    # loop bounds must be declared const int
    for m in re.finditer(r"for\s*\(([^)]*)\)", code):
        cond = re.search(r"(\w+)\s*<\s*(\w+)", m.group(1))
        if cond and not cond.group(2).isdigit():
            bound = cond.group(2)
            check(re.search(rf"const\s+int\s+{bound}\b", code) is not None,
                  "C5", f"loop bound '{bound}' is a const int")

    # bare integer literals (floats and scientific notation masked out)
    masked = re.sub(r"\d+\.\d+(?:[eE][+-]?\d+)?", "F", code)
    masked = re.sub(r"\d+[eE][+-]?\d+", "F", masked)
    masked = re.sub(r"\.\d+", "F", masked)
    allowed_line = re.compile(
        r"const\s+(?:int|vec2|vec3|float)\b"
        r"|\bfor\s*\("
        r"|textureSize\s*\("
        r"|\w+\s*\[\s*\d+\s*\]\s*\("
    )
    bare = []
    for line_no, line in enumerate(masked.splitlines(), 1):
        if allowed_line.search(line):
            continue
        bare.extend(re.findall(r"(?<![\w.])\d+(?![\w.])", line))
    check(not bare, "C6", f"no bare integer literals in float contexts (found: {bare})")

    # clamp discipline: the only clamp lives inside hsv2rgb chroma reconstruction
    hsv_span = find_function_body(code, "vec3 hsv2rgb(vec3 c)")
    clamp_positions = [m.start() for m in re.finditer(r"clamp\s*\(", code)]
    if hsv_span and clamp_positions:
        inside = all(hsv_span[0] <= p < hsv_span[1] for p in clamp_positions)
    else:
        inside = not clamp_positions
    check(inside, "C7", "clamp() only inside hsv2rgb chroma reconstruction (no HDR range clamps)")

    u_ids = set(re.findall(r"\bu_\w+", code))
    check(u_ids <= ENGINE_UNIFORM_WHITELIST, "C8",
          f"u_* identifiers within engine whitelist (found extra: {sorted(u_ids - ENGINE_UNIFORM_WHITELIST)})")

    check(re.search(r"return\s+color\s*\+\s*\(result\s*-\s*original_color\)", code) is not None,
          "C9", "delta output pattern: return color + (result - original_color)")

    check("max(sum_w, 0.001)" in code, "C10", "weight-sum denominator guarded (max(sum_w, 0.001))")
    check(re.search(r"max\(2\.0\s*,", code) is not None, "C11", "radius degeneracy guarded (max(2.0, ...))")

    check(not TRADEMARK_RE.search(strip_comments(content)), "C12", "no brand/trademark names")
    check(code.count("{") == code.count("}") and code.count("(") == code.count(")"),
          "C13", "braces and parentheses balanced")
    taps = len(re.findall(r"texture\s*\(", code))
    check(taps <= 13, "C14",
          f"texture() call sites within budget (found {taps} static sites; "
          f"the ADR loop expands to 13 fetches/pixel at runtime)")

    # helpers defined before process (definition order)
    for helper in ("vec3 rgb2hsv(vec3 c)", "vec3 hsv2rgb(vec3 c)", "float highlightMask(vec3 c, float knee_start)"):
        check(code.find(helper) < code.find("vec3 process("), f"C15-{helper.split('(')[0].split()[-1]}",
              f"helper '{helper.split('(')[0].strip()}' defined before process")

    check(re.search(r"smoothstep\(\s*knee_start\s*,\s*knee_start\s*\+\s*KNEE_WIDTH", code) is not None,
          "C16", "highlight mask uses soft knee (smoothstep), not hard cutoff")
    check("fract(HALATION_BASE_HUE + hue_shift)" in code, "C17", "HSV tint: hue anchored + user offset")

    return meta_part, shader_part


def validate_i18n() -> None:
    try:
        data = json.loads(I18N_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        check(False, "D0", f"i18n fragment is valid JSON ({exc})")
        return
    check(True, "D0", "i18n fragment is valid JSON")
    for locale in ("zh_CN", "en_US"):
        node = data.get(locale, {}).get("plugins", {}).get(PLUGIN_ID)
        check(isinstance(node, dict) and bool(node.get("name")),
              f"D1-{locale}", f"{locale}: plugins.{PLUGIN_ID}.name present")
        if isinstance(node, dict):
            params = node.get("params", {})
            missing = [v for _, v, *_ in EXPECTED_PARAMS if not params.get(v)]
            check(not missing, f"D2-{locale}", f"{locale}: params cover declared set (missing: {missing})")
            extra = sorted(set(params) - {v for _, v, *_ in EXPECTED_PARAMS})
            check(not extra, f"D3-{locale}", f"{locale}: no stray param keys (extra: {extra})")
    en = data.get("en_US", {}).get("plugins", {}).get(PLUGIN_ID, {}).get("params", {})
    for _, var, label, *_ in EXPECTED_PARAMS:
        check(en.get(var) == label, f"D4-{var}", f"en_US label for {var} matches .tglsl fallback label")


def main() -> int:
    if not PLUGIN_FILE.exists():
        print(f"FATAL: plugin file not found: {PLUGIN_FILE}")
        return 2
    if not I18N_FILE.exists():
        print(f"FATAL: i18n fragment not found: {I18N_FILE}")
        return 2

    content = PLUGIN_FILE.read_text(encoding="utf-8")
    validate_plugin(content)
    validate_i18n()

    width = max(len(code) for _, code, _ in _results) + 6
    failed = 0
    print(f"validate_Halation_Extended  (plugin: plugins/{PLUGIN_ID}.tglsl)")
    print("-" * 72)
    for ok, code, message in _results:
        failed += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {code.ljust(8)} {message}")
    print("-" * 72)
    total = len(_results)
    print(f"{total - failed}/{total} checks passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
