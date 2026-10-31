"""Plan-03 batch-1 plugin acceptance tests (12 new .tglsl, 2026-09-28).

Scope: the 12 plugins delivered for docs/plugins/2026-09-28 (Film_Grain,
Soft_Glow_Bloom, Halation_Extended, Three_Way_Tint, Bleach_Bypass,
Radial_Blur_Pro, Prism_Aberration, Vignette_Shapes, Day_Night_Converter,
Cross_Process, Skin_Even_Matte, Fake_Infrared).

Checks (P4 of plan 03):

1. Every file parses through the *real* plugin_manager path, headless, using
   the same call pattern as docs/plugins/2026-09-28/tools/validate_*.py
   (``PluginManager()._parse_plugin_file`` -- the function scan_plugins
   calls per file).
2. Required metadata exists and the plugin_id is unique and equals the
   filename stem.
3. The @param schema is complete and internally consistent (min < max,
   default inside the range, select carries options, color carries three
   0-1 components) and matches the spec parameter table shipped under
   docs/plugins/2026-09-28/specs/.
4. GLSL red lines (STYLE_NOTES section 7), statically detectable subset:
   - for-loop bounds are compile-time constants (literal / const int /
     macro parameter instantiated with integer literals); no while loops;
   - no trademark terms in the metadata surface;
   - no undeclared ``u_*`` uniforms: only the engine-injected builtins
     (u_input_tex / u_tex_size / u_aspect_ratio) and compiler-namespaced
     ``u_P*`` mentions are tolerated.

The ``clamp``-around-final-output red line is intentionally NOT asserted:
a regex probe of "final output line" is too prone to false positives (see
STYLE_NOTES section 7.3 for the allowed-clamp catalogue).

These tests are read-only: they never modify src/, plugins/ or locales/.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest

# tests/plugins/test_plugin_batch1.py -> repo root is parents[2]
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.color.core.plugin_manager import PluginManager  # noqa: E402

PLUGINS_DIR = REPO_ROOT / "plugins"
SPECS_DIR = REPO_ROOT / "docs" / "plugins" / "2026-09-28" / "specs"

# The 12 plan-03 batch-1 plugins, alphabetical (order is irrelevant).
BATCH1_IDS: List[str] = [
    "Bleach_Bypass",
    "Cross_Process",
    "Day_Night_Converter",
    "Fake_Infrared",
    "Film_Grain",
    "Halation_Extended",
    "Prism_Aberration",
    "Radial_Blur_Pro",
    "Skin_Even_Matte",
    "Soft_Glow_Bloom",
    "Three_Way_Tint",
    "Vignette_Shapes",
]

# Engine-injected uniforms allowed by STYLE_NOTES section 3. Everything
# matching the u_P* prefix is a compiler-namespaced parameter uniform
# mention (header ABI notes like ``u_P<n>_<name>``), never an identifier
# the plugin is expected to declare.
BUILTIN_UNIFORMS: Set[str] = {"u_input_tex", "u_tex_size", "u_aspect_ratio"}

VALID_PREVIEW_COSTS = {"high", "medium", "low", "skip"}

# Trademark red line (STYLE_NOTES section 7.6). Case-insensitive word
# matches; "RED" (the camera company) is checked separately, case
# sensitively, so the plain English color word "red" does not trip it.
TRADEMARK_TERMS_CI = [
    r"ARRI",
    r"\bAlexa\b",
    r"\bSony\b",
    r"\bVenice\b",
    r"\bPanasonic\b",
    r"\bVaricam\b",
    r"REDCODE",
    r"DaVinci",
    r"\bResolve\b",
    r"\bPhotoshop\b",
    r"\bLightroom\b",
    r"Kodak",
    r"Fuji",
    r"Capture\s+One",
    r"Premiere",
    r"After\s+Effects",
    r"Final\s+Cut",
    r"\bAvid\b",
    r"\bBaselight\b",
]


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def headless_parse_env(monkeypatch):
    """Make the real parsing path deterministic and side-effect free.

    - I18N stays in the untranslated/headless state (same assumption the
      docs/plugins/2026-09-28/tools/validate_*.py scripts make), so parsed
      ``name``/``label`` values are the @name / Label fallbacks regardless
      of what any earlier test loaded into the i18n singleton.
    - The effect-preview-quality manager registration (an in-memory global
      touched by _parse_plugin_file for @preview_cost plugins) is
      neutralised; the parsed preview_cost fields stay intact for asserts.
    """
    from src.shared.i18n.i18n import I18N

    monkeypatch.setattr(I18N, "_translations", {})
    monkeypatch.setattr(I18N, "_fallback_translations", {})
    monkeypatch.setattr(
        PluginManager,
        "_register_plugin_preview_quality",
        lambda self, *args, **kwargs: None,
    )


@pytest.fixture
def parsed() -> Dict[str, Dict[str, Any]]:
    """Parse all 12 plugins through the real plugin_manager code path."""
    manager = PluginManager()
    return {
        pid: manager._parse_plugin_file(str(PLUGINS_DIR / f"{pid}.tglsl"), pid)
        for pid in BATCH1_IDS
    }


def raw_content(pid: str) -> str:
    return (PLUGINS_DIR / f"{pid}.tglsl").read_text(encoding="utf-8")


def metadata_header(content: str) -> str:
    """Metadata block = everything before the closing ``// ----`` line."""
    match = re.search(r"//\s*-{5,}", content)
    assert match is not None, "closing metadata wrapper line missing"
    return content[: match.start()]


def shader_body(content: str) -> str:
    match = re.search(r"//\s*-{5,}", content)
    return content[match.end():] if match else content


def strip_line_comments(code: str) -> str:
    """Drop ``//`` comments (GLSL plugin sources carry no string literals)."""
    return "\n".join(re.sub(r"//.*", "", line) for line in code.splitlines())


def declared_param_vars(content: str) -> List[str]:
    """var names of the typed @param directives in a .tglsl header."""
    return re.findall(
        r"//\s*@param\s+(?:slider|checkbox|color|select)\s+(\w+)\s+\"", content
    )


# ---------------------------------------------------------------------------
# GLSL red-line static analysers
# ---------------------------------------------------------------------------

def loop_bound_violations(code: str) -> List[str]:
    """Red line 7.2: every for-loop bound must be a compile-time constant.

    Regex-detectable subset (fail-closed: anything unparsable is reported):
    - integer literal bounds pass;
    - identifiers declared as ``const int <name> = <int literal>;`` pass;
    - a bound that is a *macro parameter* passes only when every invocation
      of that macro in the source passes integer literals (the
      Radial_Blur_Pro ``RADIAL_ACCUMULATE(8/16/24/32)`` pattern);
    - ``while`` loops are rejected outright (no compile-time bound).
    """
    problems: List[str] = []
    stripped = strip_line_comments(code)

    if re.search(r"\bwhile\s*\(", stripped):
        problems.append("while loop present (bound can never be compile-time)")

    const_ints = {
        m.group(1)
        for m in re.finditer(
            r"\bconst\s+int\s+(\w+)\s*=\s*\d+\s*;", stripped
        )
    }
    # #define MACRO(a, b) ... -> name -> parameter list
    macros = {
        m.group(1): [p.strip() for p in m.group(2).split(",")]
        for m in re.finditer(r"#define\s+(\w+)\(([^)]*)\)", stripped)
    }

    for m in re.finditer(
        r"\bfor\s*\(([^;()]*);([^;()]*);([^;()]*)\)", stripped
    ):
        condition = m.group(2).strip()
        bound_match = re.search(r"<\s*([A-Za-z0-9_]+)\s*$", condition)
        if bound_match is None:
            # Covers `<=` and exotic conditions: report, do not guess.
            problems.append(f"unparsable for-loop condition: {condition!r}")
            continue
        bound = bound_match.group(1)
        if bound.isdigit():
            continue
        if bound in const_ints:
            continue
        accepted_by_macro = False
        for macro_name, macro_params in macros.items():
            if bound not in macro_params:
                continue
            arg_index = macro_params.index(bound)
            literal_only = True
            for inv in re.finditer(
                rf"\b{re.escape(macro_name)}\s*\(([^)]*)\)", stripped
            ):
                line_start = stripped.rfind("\n", 0, inv.start()) + 1
                if stripped[line_start:inv.start()].lstrip().startswith("#"):
                    continue  # the #define line itself, not an invocation
                args = [a.strip() for a in inv.group(1).split(",")]
                if len(args) <= arg_index or not args[arg_index].isdigit():
                    literal_only = False
                    break
            if literal_only:
                accepted_by_macro = True
        if not accepted_by_macro:
            problems.append(
                f"for-loop bound {bound!r} is not a compile-time constant "
                f"(condition {condition!r})"
            )
    return problems


def undeclared_uniforms(content: str) -> Set[str]:
    """Red line 7.7: no u_* identifier outside the engine-injected set."""
    used = set(re.findall(r"\bu_[A-Za-z0-9_]+\b", content))
    used -= BUILTIN_UNIFORMS
    used = {u for u in used if not u.startswith("u_P")}
    return used


def trademark_hits(text: str) -> List[str]:
    hits: List[str] = []
    for pattern in TRADEMARK_TERMS_CI:
        found = re.findall(pattern, text, flags=re.IGNORECASE)
        if found:
            hits.append(f"{pattern} ({found[0]!r})")
    # "RED" the camera brand is uppercase-only; lowercase "red" is a colour.
    if re.search(r"\bRED\b", text):
        hits.append("RED")
    return hits


def spec_param_vars(pid: str) -> Optional[List[str]]:
    """Extract the declared var names from the spec's parameter table.

    All 12 specs carry a level-2 ``## ... 参数 ...`` section whose rows are
    a markdown table with one cell holding the control type (slider /
    checkbox / color / select, bare or as ``@param <type>``) and one cell
    holding the backticked var name. Rows are only read inside that
    section so unrelated tables in the spec cannot pollute the result.
    """
    spec_path = SPECS_DIR / f"{pid}.md"
    if not spec_path.is_file():
        return None
    lines = spec_path.read_text(encoding="utf-8").splitlines()
    start: Optional[int] = None
    for i, line in enumerate(lines):
        if re.match(r"^##\s", line) and "参数" in line:
            start = i
            break
    if start is None:
        return None

    type_words = {"slider", "checkbox", "color", "select"}
    var_names: List[str] = []
    for line in lines[start + 1:]:
        if re.match(r"^##\s", line):
            break
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        has_type_cell = any(
            c in type_words
            or re.fullmatch(r"`?@param\s+(?:slider|checkbox|color|select)`?", c)
            for c in cells
        )
        if not has_type_cell:
            continue
        for cell in cells:
            name_match = re.fullmatch(r"`([A-Za-z_][A-Za-z0-9_]*)`", cell)
            if name_match and name_match.group(1) not in type_words:
                var_names.append(name_match.group(1))
                break
    return var_names


# ---------------------------------------------------------------------------
# 1. files / identity
# ---------------------------------------------------------------------------

def test_batch_files_exist_with_unique_ids_matching_filenames():
    """plugin_id set is unique and each id equals its filename stem."""
    paths = [PLUGINS_DIR / f"{pid}.tglsl" for pid in BATCH1_IDS]
    missing = [str(p) for p in paths if not p.is_file()]
    assert not missing, f"missing plugin files: {missing}"
    stems = [p.stem for p in paths]
    assert len(set(stems)) == len(stems) == len(BATCH1_IDS)
    assert stems == BATCH1_IDS


# ---------------------------------------------------------------------------
# 2. real-path parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pid", BATCH1_IDS)
def test_parse_via_real_plugin_manager(parsed, pid):
    """File parses through _parse_plugin_file (the scan_plugins worker)."""
    data = parsed[pid]
    assert data is not None, "parsing returned None"
    assert data["plugin_id"] == pid, "plugin_id must equal filename stem"
    assert data["type"] == "dctl"
    assert isinstance(data["params"], list) and data["params"], "no params"
    assert data["shader_code"].strip(), "shader_code is empty"
    assert re.fullmatch(r"[0-9a-f]{64}", data["content_hash"] or ""), (
        "content_hash is not a sha256 hex digest"
    )
    # Process contract: the one and only entry point.
    assert "vec3 process(vec3 color, vec2 uv)" in data["shader_code"]

    content = raw_content(pid)
    # Red line 7.8: metadata wrapped by the two // --- lines, and no
    # metadata directive leaks into the shader body (double-parse guard).
    assert content.lstrip().startswith("// --- Topos Plugin Metadata ---")
    close = re.search(r"//\s*-{5,}", content)
    assert close is not None, "closing // ---- wrapper line missing"
    assert re.search(r"//\s*@\w+\s+", content[close.end():]) is None, (
        "metadata directive leaked into shader body"
    )


# ---------------------------------------------------------------------------
# 3. required metadata
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pid", BATCH1_IDS)
def test_required_metadata_present(parsed, pid):
    data = parsed[pid]
    content = raw_content(pid)
    header = metadata_header(content)

    name_match = re.search(r"//\s*@name\s+(.+)", header)
    assert name_match, "@name directive missing"
    # Headless i18n -> parsed name is exactly the @name fallback.
    assert data["name"] == name_match.group(1).strip()

    i18n_match = re.search(r"//\s*@i18n_name\s+(.+)", header)
    assert i18n_match, "@i18n_name directive missing"
    assert i18n_match.group(1).strip() == pid, (
        "@i18n_name must equal the filename stem (STYLE_NOTES section 5)"
    )

    assert data["author"] == "Topos Team", "batch author convention"
    assert re.fullmatch(r"\d+(\.\d+)+", data["version"] or ""), (
        f"@version missing or malformed: {data['version']!r}"
    )
    assert (data.get("description") or "").strip(), "@description missing"

    # Colour plugins must declare their colour spaces (STYLE_NOTES section 4).
    caps = data.get("capabilities", {})
    assert caps.get("input_space") == "scene-linear"
    assert caps.get("output_space") == "scene-linear"

    preview_cost = data.get("preview_cost")
    assert preview_cost in (None, *VALID_PREVIEW_COSTS)
    level = data.get("preview_quality_level")
    if preview_cost is None:
        assert level is None
    else:
        assert level is not None and level.name.lower() == preview_cost


# ---------------------------------------------------------------------------
# 4. parameter schema completeness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pid", BATCH1_IDS)
def test_param_schema_complete(parsed, pid):
    params: List[Dict[str, Any]] = parsed[pid]["params"]
    assert params, "plugin declares no @param"

    seen_vars: Set[str] = set()
    for param in params:
        var = param.get("var_name")
        label = param.get("label")
        ptype = param.get("type")
        assert var and re.fullmatch(r"[a-z][a-z0-9_]*", var), (
            f"bad var_name {var!r}"
        )
        assert var not in seen_vars, f"duplicate param {var!r}"
        seen_vars.add(var)
        assert isinstance(label, str) and label.strip(), (
            f"{var}: empty label"
        )

        if ptype == "slider":
            for key in ("default", "min", "max"):
                assert key in param, f"{var}: slider missing {key}"
            assert param["min"] < param["max"], (
                f"{var}: min {param['min']} !< max {param['max']}"
            )
            assert param["min"] <= param["default"] <= param["max"], (
                f"{var}: default {param['default']} outside "
                f"[{param['min']}, {param['max']}]"
            )
        elif ptype == "checkbox":
            assert param.get("default") in (0, 1), (
                f"{var}: checkbox default must be 0/1"
            )
        elif ptype == "color":
            default = param.get("default")
            assert isinstance(default, list) and len(default) == 3, (
                f"{var}: color needs r g b defaults"
            )
            assert all(0.0 <= c <= 1.0 for c in default), (
                f"{var}: color components must be 0-1, got {default}"
            )
        elif ptype == "select":
            options = param.get("options")
            assert isinstance(options, list) and len(options) >= 2, (
                f"{var}: select needs >= 2 options"
            )
            default = param.get("default")
            assert isinstance(default, int) and 0 <= default < len(options), (
                f"{var}: select default {default!r} outside option range"
            )
        else:
            pytest.fail(f"{var}: unknown param type {ptype!r}")

    # Parsed params must be exactly the typed @param directives in the file
    # (the `@param separator` no-op is excluded by both sides).
    assert seen_vars == set(declared_param_vars(raw_content(pid)))


@pytest.mark.parametrize("pid", BATCH1_IDS)
def test_params_match_spec_table(parsed, pid):
    """Implementation exposes exactly the spec's parameter set."""
    spec_vars = spec_param_vars(pid)
    assert spec_vars, (
        f"spec {pid}.md has no parseable parameter table section"
    )
    actual = {p["var_name"] for p in parsed[pid]["params"]}
    assert actual == set(spec_vars), (
        f"spec/implementation param mismatch: "
        f"only-in-impl={sorted(actual - set(spec_vars))} "
        f"only-in-spec={sorted(set(spec_vars) - actual)}"
    )


# ---------------------------------------------------------------------------
# 5. GLSL red lines (static subset)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pid", BATCH1_IDS)
def test_for_loop_bounds_are_compile_time_constants(pid):
    problems = loop_bound_violations(shader_body(raw_content(pid)))
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("pid", BATCH1_IDS)
def test_no_undeclared_engine_uniforms(pid):
    stray = undeclared_uniforms(raw_content(pid))
    assert not stray, (
        f"undeclared u_* identifiers (P0-COL-01: breaks GL compile and "
        f"silently skips the layer): {sorted(stray)}"
    )


@pytest.mark.parametrize("pid", BATCH1_IDS)
def test_no_trademark_terms_in_metadata(pid):
    header = metadata_header(raw_content(pid))
    hits = trademark_hits(header)
    assert not hits, f"trademark terms found: {hits}"
