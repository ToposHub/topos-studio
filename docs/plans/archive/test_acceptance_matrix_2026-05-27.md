# Test Acceptance Matrix

Date: 2026-05-27
Branch: fix-ui

This document defines a tiered test acceptance matrix for the Topos Color project.
Each tier represents a progressively broader set of tests, from basic import/boundary
checks up through the full suite.

## Marker Reference

| Marker | Purpose |
|---|---|
| `modular` | Modularization boundary and smoke tests |
| `color_core` | Color pipeline safe regression tests |
| `composite_core` | Composite graph and pure node tests |
| `media_core` | Media/video pure unit tests |
| `benchmark` | Deterministic benchmark simulations |
| `visual` | Visual or PyQt screenshot tests |
| `unstable` | Known unstable tests |

---

## Tier 0: Import / Compile / Boundary

Ensures the source tree compiles and module boundaries are respected.

**Tests:**
- `tests/test_src_directory_boundaries.py`
- `python -m compileall src tests`

**Run command:**
```bash
.venv/bin/python -m compileall src tests -q
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -v
```

**Expected status: GREEN**

---

## Tier 1: Modularization Smoke

Basic smoke test confirming the modular page-context wiring is intact.

**Tests:**
- `tests/test_page_context.py`
- `tests/test_modular_import_boundaries.py`

**Run command:**
```bash
.venv/bin/python -m pytest tests/test_page_context.py tests/test_modular_import_boundaries.py -v -m "modular"
```

**Expected status: GREEN**

---

## Tier 2: Core Color Session

Color pipeline regression tests that cover session, alpha, cache, and image engine
state. These exercise the central color feature without GPU rendering.

**Tests:**
- `tests/test_color_session.py`
- `tests/test_alpha_system.py`
- `tests/test_cache_fix.py`
- `tests/test_image_engine_state.py`
- `tests/test_image_engine_layers.py`
- `tests/test_image_engine_params.py`
- `tests/test_history.py`
- `tests/test_layer.py`
- `tests/test_mask_shape.py`
- `tests/test_mask_group.py`
- `tests/test_tile.py`
- `tests/test_tiling.py`
- `tests/test_image_engine_history.py`
- `tests/test_image_engine_transforms.py`

**Run command:**
```bash
.venv/bin/python -m pytest \
  tests/test_color_session.py \
  tests/test_alpha_system.py \
  tests/test_cache_fix.py \
  tests/test_image_engine_state.py \
  tests/test_image_engine_layers.py \
  tests/test_image_engine_params.py \
  tests/test_history.py \
  tests/test_layer.py \
  tests/test_mask_shape.py \
  tests/test_mask_group.py \
  tests/test_tile.py \
  tests/test_tiling.py \
  tests/test_image_engine_history.py \
  tests/test_image_engine_transforms.py \
  -v -m "color_core"
```

**Expected status: GREEN**

---

## Tier 3: Composite Graph and Node Pure Logic

Pure-logic tests for the composite node graph, expression parser, keyframe system,
merge semantics, and transforms. No GPU, no video decode.

**Tests:**
- `tests/test_composite_graph.py`
- `tests/test_composite_preset.py`
- `tests/test_composite_project_migration.py`
- `tests/test_composite_video_frame_index.py`
- `tests/test_expression_parser.py`
- `tests/test_keyframe.py`
- `tests/test_merge_node.py`
- `tests/test_reformat_crop_transform.py`
- `tests/test_composite_bbox.py`
- `tests/test_composite_cache.py`
- `tests/test_composite_cache_key.py`
- `tests/test_composite_channels.py`
- `tests/test_composite_format.py`
- `tests/test_composite_group.py`
- `tests/test_composite_merge_semantics.py`
- `tests/test_composite_node_contract.py`
- `tests/test_composite_nodes_ai.py`
- `tests/test_composite_nodes_channel.py`
- `tests/test_composite_nodes_color.py`
- `tests/test_composite_nodes_filter.py`
- `tests/test_composite_nodes_input.py`
- `tests/test_composite_nodes_keyer.py`
- `tests/test_composite_nodes_mask.py`
- `tests/test_composite_nodes_merge.py`
- `tests/test_composite_nodes_registry.py`
- `tests/test_composite_nodes_roi_expansion.py`
- `tests/test_composite_nodes_text.py`
- `tests/test_composite_nodes_time.py`
- `tests/test_composite_nodes_transform.py`
- `tests/test_composite_nodes_utility.py`
- `tests/test_composite_pixel_level.py`
- `tests/test_composite_roi.py`
- `tests/test_composite_snapshot.py`
- `tests/test_composite_tile.py`
- `tests/test_composite_undo.py`
- `tests/test_dirty_propagation.py`
- `tests/test_deep_compositing.py`
- `tests/test_group_node.py`
- `tests/test_node_capabilities.py`
- `tests/test_node_graph_commands.py`
- `tests/test_backdrop.py`
- `tests/test_sticky_note.py`
- `tests/test_composite_mask_roto_handles.py`
- `tests/test_parallel_evaluation.py`
- `tests/test_render_scheduler.py`

**Run command:**
```bash
.venv/bin/python -m pytest \
  tests/test_composite_*.py \
  tests/test_expression_parser.py \
  tests/test_keyframe.py \
  tests/test_merge_node.py \
  tests/test_reformat_crop_transform.py \
  tests/test_dirty_propagation.py \
  tests/test_deep_compositing.py \
  tests/test_group_node.py \
  tests/test_node_capabilities.py \
  tests/test_node_graph_commands.py \
  tests/test_backdrop.py \
  tests/test_sticky_note.py \
  tests/test_parallel_evaluation.py \
  tests/test_render_scheduler.py \
  -v -m "composite_core"
```

**Expected status: GREEN**

---

## Tier 4: Media / Video Pure Unit

Pure unit tests for the media/video pipeline: decode workers, frame providers,
frame queues, and zero-copy paths. May require test fixture media files.

**Tests:**
- `tests/media/test_decode_worker.py`
- `tests/media/test_p1_1_disk_proxy_priority.py`
- `tests/media/test_macos_vt_zero_copy.py`
- `tests/media/test_color_frame_provider_adapter.py`
- `tests/media/test_video_frame_provider_service.py`
- `tests/media/test_video_frame_queue.py`
- `tests/media/test_decode_policy.py`
- `tests/media/test_disk_proxy.py`
- `tests/media/test_ffmpeg_decode.py`
- `tests/media/test_ffmpeg_seek.py`
- `tests/media/test_frame_path.py`
- `tests/media/test_gpu_frame.py`
- `tests/media/test_high_bit_depth.py`
- `tests/media/test_matrix.py`
- `tests/media/test_media_source_factory.py`
- `tests/media/test_performance_monitor.py`
- `tests/media/test_pixel_format_registry.py`
- `tests/media/test_playback_coordinator_tick.py`
- `tests/media/test_playback_sync_decode_guard.py`
- `tests/media/test_rational.py`
- `tests/media/test_resource_leak.py`
- `tests/media/test_timecode.py`
- `tests/media/test_timeline.py`
- `tests/media/test_v3_hwaccel_policy.py`
- `tests/media/test_v3_planar_playback_path.py`
- `tests/media/test_video_pipeline_smoke.py`
- `tests/media/test_video_playback_decoded_submit.py`
- `tests/media/test_video_playback_no_ui_copy.py`
- `tests/media/test_video_session_master_frame.py`
- `tests/media/test_video_session_shared_provider_real.py`
- `tests/media/test_windows_hw_zero_copy.py`
- `tests/media/test_audio_metadata.py`
- `tests/media/test_color_convert.py`
- `tests/media/test_color_decode_chroma_resize.py`
- `tests/media/test_color_metadata_mapping.py`
- `tests/media/test_source_manager_playback_sequence.py`

**Run command:**
```bash
.venv/bin/python -m pytest tests/media/ -v -m "media_core" --ignore=tests/media/test_disk_proxy_integration.py --ignore=tests/media/test_high_bit_depth_integration.py
```

**Expected status: AMBER** (some tests depend on fixture media files or hardware features)

---

## Tier 5: Benchmark Deterministic Tests

Deterministic benchmark simulations that validate latency targets, renderer queue
behavior, and UI tick responsiveness without requiring real-time playback.

**Tests:**
- `tests/benchmark/test_latency.py`
- `tests/benchmark/test_playback_performance.py`
- `tests/benchmark/test_renderer_queue.py`
- `tests/benchmark/test_ui_tick.py`

**Run command:**
```bash
.venv/bin/python -m pytest tests/benchmark/ -v -m "benchmark"
```

**Expected status: AMBER** (timing-sensitive, may fluctuate on CI)

---

## Tier 6: UI / Visual Tests

Visual regression and PyQt screenshot tests. Require a display or virtual framebuffer.

**Tests:**
- `tests/visual/test_edit_page_visual.py`

**Run command:**
```bash
.venv/bin/python -m pytest tests/visual/ -v -m "visual"
```

**Expected status: AMBER** (requires display; may need xvfb on CI)

---

## Tier 7: Full Suite

All tests. Not expected to be fully green yet; used to track overall health.

**Run command:**
```bash
.venv/bin/python -m pytest tests/ -v
```

**Expected status: RED** (full suite not yet mandatory green)

---

## Quick-Reference: Run Tiers 0-3 (gate for merge)

```bash
.venv/bin/python -m compileall src tests -q && \
.venv/bin/python -m pytest \
  tests/test_src_directory_boundaries.py \
  tests/test_page_context.py \
  tests/test_modular_import_boundaries.py \
  tests/test_color_session.py \
  tests/test_alpha_system.py \
  tests/test_cache_fix.py \
  tests/test_image_engine_state.py \
  tests/test_image_engine_layers.py \
  tests/test_image_engine_params.py \
  tests/test_composite_graph.py \
  tests/test_composite_preset.py \
  tests/test_composite_project_migration.py \
  tests/test_composite_video_frame_index.py \
  tests/test_expression_parser.py \
  tests/test_keyframe.py \
  tests/test_merge_node.py \
  tests/test_reformat_crop_transform.py \
  -v
```

---

## CI Gate Commands (Phase 13)

### Fast gate (must pass before any merge)
```bash
.venv/bin/python -m compileall -q src tests && \
.venv/bin/python -m pytest \
  tests/test_src_directory_boundaries.py \
  tests/test_page_context.py \
  tests/test_color_session.py \
  tests/test_composite_graph.py \
  -q
```

### Color gate
```bash
.venv/bin/python -m pytest \
  tests/test_color_session.py \
  tests/test_alpha_system.py \
  tests/test_cache_fix.py \
  tests/test_image_engine_state.py \
  tests/test_image_engine_layers.py \
  tests/test_image_engine_params.py \
  tests/test_color_tools_params.py \
  tests/test_color_tools_scripting_api.py \
  tests/test_color_tools_lut_exporter.py \
  -q
```

### Composite gate
```bash
.venv/bin/python -m pytest \
  tests/test_composite_graph.py \
  tests/test_composite_preset.py \
  tests/test_composite_project_migration.py \
  tests/test_composite_video_frame_index.py \
  tests/test_expression_parser.py \
  tests/test_keyframe.py \
  tests/test_merge_node.py \
  tests/test_reformat_crop_transform.py \
  tests/test_composite_nodes_merge.py \
  tests/test_composite_nodes_text.py \
  tests/test_composite_roi.py \
  tests/test_composite_tile.py \
  tests/test_deep_compositing.py \
  tests/test_node_capabilities.py \
  tests/test_node_graph_commands.py \
  -q
```

### Media gate
```bash
.venv/bin/python -m pytest \
  tests/media/test_decode_worker.py \
  tests/media/test_p1_1_disk_proxy_priority.py \
  tests/media/test_macos_vt_zero_copy.py \
  tests/media/test_video_frame_provider_service.py \
  tests/media/test_video_frame_queue.py \
  -q
```

### Benchmark gate
```bash
.venv/bin/python -m pytest \
  tests/benchmark/test_latency.py \
  tests/benchmark/test_playback_performance.py \
  tests/benchmark/test_renderer_queue.py \
  tests/benchmark/test_ui_tick.py \
  -q
```

### Non-GL full gate (excludes tests requiring real OpenGL context)
```bash
.venv/bin/python -m pytest tests \
  --ignore=tests/visual \
  --ignore=tests/test_yuv_gpu_upload.py \
  --ignore=tests/test_yuv_temp_texture_release.py \
  --ignore=tests/test_yuv_upload_api.py \
  --ignore=tests/test_video_texture_pool.py \
  --ignore=tests/test_video_upload_command.py \
  --ignore=tests/test_yuv_chroma_sampling_pixels.py \
  -q
```

### Visual gate (only after visual tests are stabilized)
```bash
.venv/bin/python -m pytest tests/visual -q
```

---

## Current Status (2026-05-27)

| Gate | Status | Count |
|------|--------|-------|
| Fast gate | GREEN | ~101 passed |
| Color gate | GREEN | ~120 passed, 4 skipped |
| Composite gate | GREEN | 521 passed |
| Media gate | GREEN | ~100+ passed |
| Benchmark gate | GREEN | ~13 passed |
| Feature flags | GREEN | new |
| Full non-GL | AMBER | 4738 passed, ~30-50 failed (export/UI) |
| Visual gate | NOT RUN | requires display |
