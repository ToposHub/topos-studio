#!/usr/bin/env python3
"""Validate plugins/Film_Grain.tglsl against the real plugin_manager parsing path.

Headless: imports PluginManager (pure-Python deps only) and calls the internal
_parse_plugin_file used by scan_plugins. Read-only on the repository; writes
stdout only (exit code 0 = all checks pass).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List

# docs/plugins/2026-09-28/tools/validate_Film_Grain.py -> repo root is parents[4]
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from src.features.color.core.plugin_manager import PluginManager  # noqa: E402

PLUGIN_FILE = REPO_ROOT / "plugins" / "Film_Grain.tglsl"
PLUGIN_ID = "Film_Grain"

EXPECTED_PARAMS = [
    # var, label-fallback, default, min, max
    ("strength", "Strength", 0.35, 0.0, 1.0),
    ("grain_size", "Grain Size", 1.5, 1.0, 8.0),
    ("shadow_bias", "Shadow Bias", 0.25, 0.0, 1.0),
    ("highlight_bias", "Highlight Bias", 0.15, 0.0, 1.0),
]

failures: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    line = f"[{'PASS' if cond else 'FAIL'}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line)
    if not cond:
        failures.append(name)


def main() -> int:
    print(f"repo root : {REPO_ROOT}")
    print(f"plugin    : {PLUGIN_FILE}")
    print(f"exists    : {PLUGIN_FILE.is_file()}")
    print("-" * 60)

    # --- parse via the real path (same function scan_plugins calls) ---
    pm = PluginManager()
    data: Dict[str, Any] | None = pm._parse_plugin_file(str(PLUGIN_FILE), PLUGIN_ID)
    check("parse succeeds (non-None)", data is not None)
    if data is None:
        print("FATAL: parse returned None")
        return 1

    # --- metadata ---
    # Headless: no locale loaded -> I18N.tr returns the @name / Label fallbacks.
    check("name == 'Film Grain'", data["name"] == "Film Grain", repr(data["name"]))
    check("author == 'Topos Team'", data["author"] == "Topos Team", repr(data["author"]))
    check("version == '1.0'", data["version"] == "1.0", repr(data["version"]))
    check("description non-empty", bool((data.get("description") or "").strip()))
    check("preview_cost undeclared (light effect)", data.get("preview_cost") is None)

    # --- capability manifest ---
    caps = data.get("capabilities", {})
    for key, want in [
        ("input_space", "scene-linear"),
        ("output_space", "scene-linear"),
        ("precision", "float16"),
        ("determinism", "deterministic"),
        ("export_support", "video"),
        ("occlusion", "background_independent"),
    ]:
        check(f"capability {key} == {want}", caps.get(key) == want, repr(caps.get(key)))

    # --- params schema ---
    params = data.get("params", [])
    check("param count == 4", len(params) == 4, f"got {len(params)}")
    by_var = {p.get("var_name"): p for p in params}
    check("param var set matches spec",
          set(by_var) == {v for v, *_ in EXPECTED_PARAMS},
          str(sorted(by_var)))
    for var, label, dflt, lo, hi in EXPECTED_PARAMS:
        p = by_var.get(var)
        if p is None:
            continue
        check(f"{var}: type slider", p.get("type") == "slider", repr(p.get("type")))
        check(f"{var}: label fallback == '{label}'", p.get("label") == label, repr(p.get("label")))
        check(f"{var}: default == {dflt}", p.get("default") == dflt, repr(p.get("default")))
        check(f"{var}: min == {lo}", p.get("min") == lo, repr(p.get("min")))
        check(f"{var}: max == {hi}", p.get("max") == hi, repr(p.get("max")))
        if all(k in p for k in ("default", "min", "max")):
            check(f"{var}: min <= default <= max", p["min"] <= p["default"] <= p["max"])

    # --- shader code ---
    code = data.get("shader_code") or ""
    check("shader_code non-empty", bool(code.strip()))
    check("process entry point present",
          "vec3 process(vec3 color, vec2 uv)" in code)
    check("content_hash is 64-hex",
          bool(re.fullmatch(r"[0-9a-f]{64}", data.get("content_hash") or "")))

    # --- red lines (static guards) ---
    # 1. No undeclared u_* uniforms: only the injected u_tex_size is allowed.
    used = set(re.findall(r"\bu_\w+\b", code))
    check("no undeclared u_* uniforms (only u_tex_size)", used <= {"u_tex_size"},
          f"found: {sorted(used)}")
    # 2. Metadata directives must not leak into shader body (would double-parse).
    check("no @param directive inside shader body",
          re.search(r"//\s*@param\s", code) is None)
    content = PLUGIN_FILE.read_text(encoding="utf-8")
    check("exactly 4 @param directives in whole file",
          len(re.findall(r"//\s*@param\s+(\w+)\s+(\w+)\s+\"", content)) == 4)
    # 3. Wrapper lines present, all metadata before the closing ---- line.
    check("metadata wrapped by // --- header line",
          content.lstrip().startswith("// --- Topos Plugin Metadata ---"))
    close = re.search(r"//\s*-{5,}", content)
    check("closing // ----------------------------- line present", close is not None)
    if close:
        check("all @ directives before closing line",
              "@" not in content[close.end():].replace("@", "@", 1) or
              not re.search(r"//\s*@\w+\s+", content[close.end():]))
    # 4. No loops (so no non-const loop bound risk), no expensive builtins.
    check("no for/while loops", not re.search(r"\b(for|while)\s*\(", code))
    check("no sin/cos/pow/exp/log builtins",
          not re.search(r"\b(sin|cos|pow|exp|log)\s*\(", code))
    # 5. Early exit first statement.
    check("first-line early exit on strength",
          "if (strength < 0.001) return color;" in code)
    # 6. No clamp applied to the output color statement.
    check("return statement not clamped",
          not re.search(r"return\s+clamp\s*\(", code))
    # 7. Every float literal carries a decimal point in obvious numeric
    #    contexts (vector constructors / arithmetic). Heuristic: integer
    #    literals 2-999 adjacent to '.' or inside vec*( ) args are checked by
    #    eye; here assert the known-bad bare-int-in-float-op patterns.
    check("no bare int in float ops (x * 2 style)",
          not re.search(r"[*+\-/]\s*\d+\s*[*+\-/)]", code.replace("//", "")))

    print("-" * 60)
    total = len(failures)
    if total:
        print(f"RESULT: FAIL ({total} checks failed)")
        return 1
    print("RESULT: PASS (all checks passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
