# Validation Status (2026-05-27)

> Updated after all phases complete: A/B/C/D/E/F/G + post-validation cleanup.
>
> **Core Issues Resolved; Cleanup Tail Addressed.** See `docs/post_validation_remaining_cleanup_plan_2026-05-27.md` for the cleanup plan.

## Test Results Summary

| Category | Result | Notes |
|----------|--------|-------|
| Compile | PASS | `compileall src tests` clean |
| Boundaries / Feature Flags / Markers | 158 passed | markers, flags, modular imports, budget |
| Color session + engine | 125 passed | core color pipeline |
| Color tools (LUT, scripting) | 97 passed, 4 skipped | LUT exporter, params, API |
| Composite logic | 1028 passed, 85 warnings | graph, presets, nodes, evaluation |
| Media core | 1377 passed, 44 skipped | decode, proxy, frame provider |
| YUV/GPU upload | 77 passed | no GL context issues on this machine |
| Export / FFmpeg / Save | 173 passed | frame provider, encoder, router |
| Sticky note / Backdrop / HUD | 89 passed | UI component tests |
| Visual tests | 3 passed, 15 skipped | no segfault; skip when no display |
| Benchmark | 16 passed | latency, playback, queue, tick |
| Acceptance markers | 5 passed | marker registration verified |

## Architecture Summary

| Component | Lines | Description |
|-----------|-------|-------------|
| `main_window.py` | 931 | Shell: __init__, create_panels, setup_connections |
| `edit_controller.py` | 1417 | Timeline/clip/effect management |
| `file_controller.py` | 1717 | File/video/events/close/backup |
| `layer_controller.py` | 1004 | Layer/render/grading operations |
| `menu_toolbar_controller.py` | 1179 | Menus/toolbar/shortcuts |
| `dock_layout_controller.py` | 1073 | Dock/layout management |
| `deliver_controller.py` | 663 | Export/deliver |
| `color_controller.py` | 946 | Masks/LUT/denoise/crop |
| `page_controller.py` | 709 | Page switching/context |
| `composite_controller.py` | 217 | Composite/nodes |
| **Total controllers** | **8925** | 9 mixin classes |

## Marker Collection

| Marker | Tests Collected | Status |
|--------|----------------|--------|
| `color_core` | 341 | Working |
| `composite_core` | 1325 | Working |
| `media_core` | 1421 | Working |
| `modular` | 82 | Working |
| `visual` | 18 | Working |

## Core Issues Resolved

### Phase A: Marker Gates
- `pytest_collection_modifyitems` in `tests/conftest.py` auto-assigns markers by file path
- `-m color_core/composite_core/media_core/modular` no longer 0 selected
- Added `tests/test_acceptance_markers.py` for marker verification

### Phase B: Visual Segfault
- Added `test_mode=False` parameter to `MainWindow.__init__`
- `test_mode=True` skips `create_panels()`, `setup_docks()`, `setup_menus()`, etc.
- Visual tests now skip gracefully (pytest.skip) instead of crashing
- Added `tests/visual/test_visual_bootstrap.py` for smoke testing

### Phase C: MainWindow Controller Extraction — COMPLETE
- Extracted all 9 controller mixins from MainWindow
- MainWindow reduced from **9219 to 931 lines** (90% reduction)
- All deep panel imports deferred to `create_panels()` to avoid QWebEngine timing issues
- Module-level deep feature imports reduced to 6 (controller bridge files + ImageEngine init)

### Phase D: Feature Flag Unification
- `src/shared/feature_flags.py` is canonical (dataclass-based `FeatureFlags`)
- Supports both `TOPOS_FEATURE_*` and `TOPOS_DISABLE_FEATURES` env vars
- **Priority:** Explicit `TOPOS_FEATURE_*` overrides `TOPOS_DISABLE_FEATURES` bulk disable
- `src/app/feature_flags.py` is a thin re-export wrapper for backward compat
- `color` feature cannot be disabled (core functionality)

### Phase E: Deep Import Budget
- Budget-based test in `tests/test_src_directory_boundaries.py`
- Current budget: 85, actual count: 79 (mostly deferred imports in controller bridge files)
- Budget tightened from initial 100; module-level deep imports reduced to 6

### Phase F: Cache Paths
- `CACHE_DIR` moved from `src/.images_cache` to `<project>/.cache/topos-color/images_cache`
- `BAKED_CACHE_DIR` moved to `<project>/.cache/topos-color/baked_layers`
- Running tests no longer creates `src/.images_cache`
- Old `.images_cache/` and `src/.images_cache/` added to `.gitignore`

### Phase G: Documentation
- Created and maintained `docs/validation_status_2026-05-27.md`
- All test numbers verified and up-to-date

## Remaining Cleanup Tail

See `docs/post_validation_remaining_cleanup_plan_2026-05-27.md` for full details.
All P0/P1 items have been addressed in the post-validation cleanup commits.

## Additional Fixes During Phase C Completion

- **super() MRO fix**: Changed `super().closeEvent()` / `super().resizeEvent()` in mixins to `QMainWindow.closeEvent(self, event)` to avoid MRO resolution errors with multiple mixins
- **QWebEngine import timing**: Moved all panel imports from module-level to inside `create_panels()` to prevent QWebEngine from being imported before QApplication
- **ShortcutDialog deferred import**: Moved to method-level in menu_toolbar_controller.py
- **Early QWebEngine import in conftest**: Added to `tests/conftest.py` as safety net
- **get_plugin_manager import**: Added missing import in menu_toolbar_controller.py
