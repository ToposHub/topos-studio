"""Registry-level tests for the plan-03 batch-1 plugins (2026-09-28).

Covers the three registry invariants of the batch:

1. The 12 plugin ids are unique (and each equals its filename stem).
2. ``PluginManager.scan_plugins`` discovers every ``.tglsl`` in the
   plugin directory. The scan is executed against a *temporary copy* of
   ``plugins/`` so the singleton's in-memory registry and the global
   effect-preview-quality manager are never polluted with repo paths
   (scan_plugins registers @preview_cost plugins globally); singleton
   state is snapshotted and restored around the scan regardless.
3. The live i18n locales expose the ``plugins.<id>`` namespace for all 12
   ids in both zh_CN and en_US, with a ``name`` entry and one
   ``params.<var>`` label per declared @param (exact set match, so drift
   in either direction is caught). The locale JSON files are read
   directly -- ``I18N.load_locale`` is never called, keeping the i18n
   singleton untouched.

Read-only with respect to src/, plugins/ and locales/.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pytest

# tests/plugins/test_plugin_registry.py -> repo root is parents[2]
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.color.core.plugin_manager import PluginManager  # noqa: E402

PLUGINS_DIR = REPO_ROOT / "plugins"
LOCALES_DIR = REPO_ROOT / "src" / "shared" / "i18n" / "locales"
DOC_I18N_DIR = REPO_ROOT / "docs" / "plugins" / "2026-09-28" / "i18n"

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

REQUIRED_LOCALES = ["zh_CN", "en_US"]


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def headless_parse_env(monkeypatch):
    """Keep the parsing path headless and free of global side effects."""
    from src.shared.i18n.i18n import I18N

    monkeypatch.setattr(I18N, "_translations", {})
    monkeypatch.setattr(I18N, "_fallback_translations", {})
    monkeypatch.setattr(
        PluginManager,
        "_register_plugin_preview_quality",
        lambda self, *args, **kwargs: None,
    )


def declared_param_vars(pid: str) -> List[str]:
    content = (PLUGINS_DIR / f"{pid}.tglsl").read_text(encoding="utf-8")
    return re.findall(
        r"//\s*@param\s+(?:slider|checkbox|color|select)\s+(\w+)\s+\"", content
    )


@pytest.fixture
def scanned_plugins(tmp_path) -> Tuple[List[Path], Dict[str, Dict[str, Any]]]:
    """scan_plugins over an isolated temp copy of every top-level .tglsl.

    scan_plugins joins its argument onto the project root, and
    ``os.path.join`` yields the argument untouched when it is absolute --
    so passing the temp dir scans exactly the copies. The singleton's
    plugin dict is snapshotted and restored afterwards.
    """
    tglsl_files = sorted(p for p in PLUGINS_DIR.iterdir() if p.suffix == ".tglsl")
    assert tglsl_files, "plugins/ contains no .tglsl files"
    for src in tglsl_files:
        shutil.copy(src, tmp_path / src.name)

    manager = PluginManager()
    saved = dict(manager.plugins)
    try:
        # scan_plugins merges into the singleton and never evicts, so any
        # entry registered by an earlier test (builtin .tai plugins, other
        # tests' tmp fixtures) would leak into our result. Clear first to
        # make the scan authoritative for the temp dir and the test
        # order-independent; snapshot is restored in the finally below.
        manager.plugins.clear()
        # scan_plugins returns self.plugins itself -- snapshot it so the
        # restore below cannot empty the result handed to the test.
        result = dict(manager.scan_plugins(str(tmp_path)))
    finally:
        manager.plugins.clear()
        manager.plugins.update(saved)
    return tglsl_files, result


def load_locale(locale: str) -> Dict[str, Any]:
    path = LOCALES_DIR / f"{locale}.json"
    assert path.is_file(), f"locale file missing: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. id uniqueness
# ---------------------------------------------------------------------------

def test_batch_plugin_ids_unique():
    assert len(BATCH1_IDS) == 12
    assert len(set(BATCH1_IDS)) == 12, "duplicate plugin ids in batch"
    for pid in BATCH1_IDS:
        path = PLUGINS_DIR / f"{pid}.tglsl"
        assert path.is_file(), f"missing {path}"
        assert path.stem == pid


# ---------------------------------------------------------------------------
# 2. scan coverage (isolated temp copy of plugins/)
# ---------------------------------------------------------------------------

def test_scan_plugins_discovers_every_tglsl(scanned_plugins):
    tglsl_files, result = scanned_plugins
    expected = {p.stem for p in tglsl_files}
    assert set(result) == expected, (
        f"scan missed {sorted(expected - set(result))}, "
        f"unexpected {sorted(set(result) - expected)}"
    )
    # scan_plugins keys the registry by filename stem; plugin data must agree.
    for stem, data in result.items():
        assert data is not None and data["plugin_id"] == stem


def test_scan_plugins_parses_batch1_plugins(scanned_plugins):
    _, result = scanned_plugins
    missing = [pid for pid in BATCH1_IDS if pid not in result]
    assert not missing, f"scan_plugins missed batch plugins: {missing}"
    for pid in BATCH1_IDS:
        data = result[pid]
        assert data["shader_code"].strip(), f"{pid}: empty shader_code"
        assert data["params"], f"{pid}: no params parsed"
        assert {p["var_name"] for p in data["params"]} == set(
            declared_param_vars(pid)
        ), f"{pid}: parsed params differ from typed @param directives"


# ---------------------------------------------------------------------------
# 3. live i18n locales
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("locale", REQUIRED_LOCALES)
def test_locale_has_all_batch_plugin_namespaces(locale):
    """All 12 plugins.<id> namespaces exist in the locale file."""
    plugins_ns = load_locale(locale).get("plugins")
    assert isinstance(plugins_ns, dict), f"{locale}: no 'plugins' namespace"
    missing = [pid for pid in BATCH1_IDS if pid not in plugins_ns]
    assert not missing, f"{locale}: missing namespaces {missing}"


@pytest.mark.parametrize("pid", BATCH1_IDS)
@pytest.mark.parametrize("locale", REQUIRED_LOCALES)
def test_locale_namespace_shape_and_param_coverage(pid, locale):
    """namespace has a name entry and one params.<var> per declared param."""
    plugins_ns = load_locale(locale)["plugins"]
    namespace = plugins_ns[pid]

    name = namespace.get("name")
    assert isinstance(name, str) and name.strip(), (
        f"{locale}: plugins.{pid}.name missing/empty"
    )

    params_ns = namespace.get("params")
    assert isinstance(params_ns, dict), f"{locale}: plugins.{pid}.params missing"
    declared = set(declared_param_vars(pid))
    assert set(params_ns) == declared, (
        f"{locale}: plugins.{pid}.params drift: "
        f"missing={sorted(declared - set(params_ns))} "
        f"extra={sorted(set(params_ns) - declared)}"
    )
    empty = [
        var
        for var, text in params_ns.items()
        if not (isinstance(text, str) and text.strip())
    ]
    assert not empty, f"{locale}: plugins.{pid}.params empty entries {empty}"


# ---------------------------------------------------------------------------
# 4. docs i18n fragments (deliverable completeness, shape-tolerant)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pid", BATCH1_IDS)
def test_docs_i18n_fragment_covers_both_locales(pid):
    """docs/plugins/2026-09-28/i18n/<id>.json carries zh_CN + en_US content.

    Two fragment shapes ship with the batch and both are accepted:
    A) {<pid>: {name/params keyed by locale}}  (Film_Grain)
    B) {<locale>: {plugins: {<pid>: {name, params/desc}}}}  (the other 11)
    """
    path = DOC_I18N_DIR / f"{pid}.json"
    assert path.is_file(), f"docs i18n fragment missing: {path}"
    fragment = json.loads(path.read_text(encoding="utf-8"))

    per_locale: Dict[str, Dict[str, Any]] = {}
    if pid in fragment and isinstance(fragment[pid], dict):
        # Shape A: entries keyed by plugin id, values keyed by locale.
        block = fragment[pid]
        for locale in REQUIRED_LOCALES:
            entry = {}
            name = block.get("name")
            if isinstance(name, dict):
                entry["name"] = name.get(locale)
            params = block.get("params")
            if isinstance(params, dict):
                entry["params"] = {
                    var: value.get(locale)
                    for var, value in params.items()
                    if isinstance(value, dict)
                }
            per_locale[locale] = entry
    else:
        # Shape B: top-level locale keys mirroring the live locale layout.
        for locale in REQUIRED_LOCALES:
            per_locale[locale] = fragment.get(locale, {}).get("plugins", {}).get(
                pid, {}
            )

    for locale in REQUIRED_LOCALES:
        entry = per_locale.get(locale) or {}
        name = entry.get("name")
        assert isinstance(name, str) and name.strip(), (
            f"{pid}.json: {locale} name missing/empty"
        )
        # Param labels are optional in the fragment when the live locale
        # already carries them, but when present they must not be empty.
        labels = entry.get("params") or {}
        empty = [
            var
            for var, text in labels.items()
            if not (isinstance(text, str) and text.strip())
        ]
        assert not empty, f"{pid}.json: {locale} empty param labels {empty}"
