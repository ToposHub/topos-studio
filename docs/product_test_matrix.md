# Product Test Matrix

## Product Modes

| Product | Enabled Features | Disabled Features |
|---|---|---|
| topos-studio | color, edit, composite, deliver, library | (none) |
| topos-color | color, deliver, library | edit, composite |
| topos-edit | color, edit, deliver, library | composite |
| topos-composite | color, composite, deliver, library | edit |

## Test Matrix Per Product

### topos-studio (full product)

| Area | Test | Marker |
|---|---|---|
| Features | All 5 features enabled | studio_product |
| Panels | All panel categories visible | - |
| Menus | All feature menu actions present | - |
| Shortcuts | All feature shortcuts active | - |
| Preferences | All preference pages visible | - |
| Packaging | No excluded dirs | - |

### topos-color (color grading focused)

| Area | Test | Marker |
|---|---|---|
| Features | color + deliver + library enabled | color_product |
| Panels | Only Color, Workspace, Deliver categories | - |
| Menus | No edit/composite actions | - |
| Shortcuts | No edit/composite shortcuts | - |
| Preferences | No edit/composite pages | - |
| Packaging | edit/ and composite/ dirs excluded | - |

### topos-edit (editing focused)

| Area | Test | Marker |
|---|---|---|
| Features | edit + color + deliver + library enabled | edit_product |
| Panels | No Composite category | - |
| Menus | No composite actions | - |
| Shortcuts | No composite shortcuts | - |
| Preferences | No composite pages | - |
| Packaging | composite/ dir excluded | - |

### topos-composite (compositing focused)

| Area | Test | Marker |
|---|---|---|
| Features | composite + color + deliver + library enabled | composite_product |
| Panels | No Edit category | - |
| Menus | No edit actions | - |
| Shortcuts | No edit shortcuts | - |
| Preferences | No edit pages | - |
| Packaging | edit/ dir excluded | - |

## Acceptance Commands

```bash
# Compile check
.venv/bin/python -m compileall -q src tests

# Guardrail tests (no GUI required)
.venv/bin/python -m pytest tests/test_boundary_guardrails.py -q

# Product presets and packaging
.venv/bin/python -m pytest tests/test_product_presets.py -q

# Feature flags
.venv/bin/python -m pytest tests/test_feature_flags.py -q

# Boundary imports
.venv/bin/python -m pytest tests/test_boundary_imports.py -q

# All modular boundary tests
.venv/bin/python -m pytest -m modular -q

# Build product bundle (placeholder)
scripts/build_product.sh topos-color

# Verify product bundle
scripts/verify_product_bundle.py topos-color .
```

## Review Checklist

- [ ] Color feature is enabled in every product preset
- [ ] Library feature is enabled in every product preset
- [ ] No forbidden deep imports in src/app or src/shared
- [ ] Each product entry point has register() and get_preset()
- [ ] Packaging config include/exclude match preset resolved sets
- [ ] i18n locale files contain all menu.panels.* keys
- [ ] All registries (actions, shortcuts, preferences, panels) filter by enabled_features
- [ ] Optional module registration survives missing feature directories (try/except ImportError)
- [ ] pyproject.toml has 4 console_scripts entry points
- [ ] build_product.sh validates product name before proceeding
