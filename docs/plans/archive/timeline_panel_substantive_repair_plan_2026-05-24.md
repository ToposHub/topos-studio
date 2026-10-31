# Timeline Panel Substantive Repair Plan

Date: 2026-05-24

This document is a practical repair plan for the Edit page timeline panel. It focuses on the user-visible failures:

- Timeline ruler / scale is invisible or misaligned.
- Clip visual duration on the timeline does not match real media duration.
- Playback in Program Viewer becomes blank / white / no-clip after timeline edits.
- Audio waveform is missing, stale, or drawn against the wrong time range.

The goal is not a visual polish pass. The goal is to make the timeline model, timeline view, preview renderer, and waveform renderer agree on the same frame/time coordinate system.

---

## 1. Core Diagnosis

### 1.1 Timeline Coordinate System Is Inconsistent

Relevant files:

- `src/ui/edit/timeline_view.py`
- `src/ui/edit/timeline_items.py`
- `src/ui/edit/timeline_panel.py`

Current risk:

- The timeline has three coordinate zones:
  - Ruler/header zone.
  - Track content zone.
  - Empty area below tracks.
- Some hit testing and drawing code treats scene x/y as if:
  - Frame 0 starts at x = 0.
  - Track 0 starts at y = 0.
  - Every track has the same fixed height.
- But the actual UI uses:
  - `TRACK_HEADER_WIDTH` as the x offset before clips/ruler ticks.
  - `TRACK_AREA_TOP` / ruler height as the y offset before tracks.
  - Different video/audio track heights.

Result:

- Ruler/grid ticks can render under the wrong x range.
- Drops/scrubs in the ruler area can be interpreted as V1.
- Audio tracks can be positioned using fixed video height.
- Scene rect can be too small, clipping or hiding ruler/track content.

Required fix:

Create one source of truth in `TimelineView`:

- `_ordered_tracks() -> tuple[EditTrack, ...]`
- `_track_height_for(track) -> int`
- `_track_top_for_index(index) -> float`
- `_track_index_at_y(y) -> int`
- `_total_track_area_height() -> int`
- `_layout_track_items() -> None`

All timeline visual positioning must go through these helpers.

Acceptance:

- Ruler area hit test returns `-1`.
- V1 hit test returns `0`.
- A1 hit test returns `len(video_tracks)`.
- Audio clips sit at the same y as their audio track header.
- Zooming does not change track y positions.

---

## 2. Timeline Ruler / Scale Repair

### 2.1 Ruler Duration Must Use Effective Timeline Duration

Relevant files:

- `src/ui/edit/timeline_view.py`
- `src/ui/edit/timeline_items.py`
- `src/ui/edit/timeline_panel.py`

Problem:

If the sequence has no content, `duration_frames` may be 0. If ruler duration is 0, the ruler item can appear blank or too short.

Required fix:

- Add or reuse an effective duration helper:
  - At least current timeline duration.
  - At least playback controller duration.
  - At least a visible minimum, such as 10 seconds at sequence fps.
- Use that effective duration when:
  - Creating/updating `TimecodeRulerItem`.
  - Updating `sceneRect`.
  - Drawing background grid.
  - Updating `TimelinePanel` playback duration.

Implementation notes:

- In `drawBackground`, grid lines should use:
  - `x = TRACK_HEADER_WIDTH + frame * pixels_per_frame`
  - not `x = frame * pixels_per_frame`.
- Visible frame range should subtract `TRACK_HEADER_WIDTH` before converting pixels to frames.
- Grid vertical line height should span the real cumulative track area, not `num_tracks * TRACK_HEIGHT`.

Acceptance:

- New empty timeline still shows visible ruler ticks.
- Ruler ticks align with clip left edges.
- Clip at timeline frame 0 starts exactly after track header.

---

## 3. Clip Visual Duration Repair

### 3.1 Separate Timeline Frames From Source Frames

Relevant files:

- `src/ui/edit/timeline_view.py`
- `src/core/edit/timeline_v2.py`
- `src/core/edit/timeline_render_plan.py`

Problem:

`timeline_duration` and `source_duration` are different domains:

- `timeline_duration`: frames in the sequence fps.
- `source_duration`: frames in the media source fps.

If a 30fps, 10s clip is inserted into a 24fps timeline:

- Timeline visual duration should be 240 frames.
- Source duration should usually be 300 source frames.
- Playback speed / mapping should be 300 / 240 = 1.25 source frames per timeline frame.

Common bug:

Using `asset.total_frames` directly as `timeline_duration` makes the clip visually too long in lower-fps timelines.

Required fix:

In `TimelineView`:

- `_get_asset_duration_frames(asset)` returns timeline frames:
  - Prefer `duration_seconds * timeline_fps`.
  - Else convert `total_frames * timeline_fps / asset_fps`.
  - Else fallback to still-image duration.
- `_get_asset_source_duration_frames(asset)` returns native source frames:
  - Prefer `total_frames`.
  - Else `duration_seconds * asset_fps`.
  - Else fallback to timeline duration.
- `_get_asset_playback_speed(asset, timeline_duration, source_duration)` returns:
  - `source_duration / timeline_duration`, clamped to sane bounds.

When creating `ClipInstance`:

- `timeline_duration = timeline_duration_frames`
- `source_duration = source_duration_frames`
- `speed = source_duration_frames / timeline_duration_frames`

Acceptance:

- 10s 30fps media in 24fps sequence displays as 10 seconds on the ruler.
- Render plan maps timeline frame 239 to source frame near 299.
- Trimming uses source duration for media boundaries, not timeline duration.

---

## 4. Program Viewer Blank / White Playback Repair

### 4.1 Timeline Edits Must Immediately Refresh Preview Renderer

Relevant files:

- `src/ui/main_window.py`
- `src/ui/edit/edit_page.py`
- `src/core/edit/timeline_preview_renderer.py`
- `src/ui/edit/program_viewer.py`

Problem:

When `TimelineView` emits `timeline_changed`, the renderer may receive the new sequence but the Program Viewer can still display a stale previous result:

- Previous empty timeline frame.
- Previous "no clip" state.
- Previous render error.
- Old renderer still connected to frame request signals.

Required fix in `MainWindow._on_edit_timeline_changed`:

- Update `edit_page.set_timeline(timeline)`.
- Update `_current_timeline_sequences[timeline.timeline_id]`.
- Call `timeline_preview_renderer.set_sequence(timeline)`.
- Immediately request the current playhead frame.

Add helper:

```python
def _request_current_timeline_preview_frame(self) -> None:
    if self.timeline_preview_renderer is None:
        return
    frame = self.timeline_panel.get_playhead_frame()
    self.timeline_preview_renderer.request_render(max(0, int(frame)))
```

### 4.2 Renderer Replacement Must Disconnect Old Signals

Problem:

`_init_timeline_preview_renderer()` shuts down the old renderer, but signal connections may remain:

- `edit_page.timeline_frame_requested -> old_renderer.request_render`
- `old_renderer.frame_ready -> main_window._on_timeline_preview_frame_ready`
- `old_renderer.render_error -> main_window._on_timeline_preview_render_error`
- `program_viewer.viewport_resized -> main_window._on_program_viewer_viewport_resized`

Risk:

- Requests go to a shutdown renderer.
- Old renderer emits stale `None` / error frame.
- Multiple connections create duplicate renders and unpredictable UI state.

Required fix:

Before shutting down old renderer:

```python
def _disconnect_timeline_preview_renderer_signals(self, renderer: object) -> None:
    connections = (
        (self.edit_page.timeline_frame_requested, renderer.request_render),
        (renderer.frame_ready, self._on_timeline_preview_frame_ready),
        (renderer.render_error, self._on_timeline_preview_render_error),
        (self.program_viewer_widget.viewport_resized, self._on_program_viewer_viewport_resized),
    )
    for signal, slot in connections:
        try:
            signal.disconnect(slot)
        except (TypeError, RuntimeError):
            pass
```

Then:

```python
old_renderer.shutdown()
old_renderer.deleteLater()
self.timeline_preview_renderer = None
```

### 4.3 Renderer Must Drop Stale Frames From Old Generations

Relevant file:

- `src/core/edit/timeline_preview_renderer.py`

Problem:

Even after `set_sequence`, an old queued frame can finish later and emit a `None` result. Program Viewer then switches back to "no clip" or blank.

Required fix:

- Increment `_generation_id` in `set_sequence()` and `set_project()`.
- Queue render tasks with generation id.
- Worker checks task generation before and after expensive decode/composite work.
- Worker emits `frame_rendered` / `render_error` only if task generation still matches.
- Clear `_pending_frame` on `set_sequence()` and `set_project()`.

Acceptance:

- Add clip to empty timeline: Program Viewer immediately renders the clip under playhead.
- Rapid drag/drop/replace does not flash back to no-clip after a later stale worker result.
- Reopening/changing project does not leave a previous renderer responding.

---

## 5. Audio Waveform Repair

### 5.1 Waveform Time Range Must Use Correct FPS

Relevant files:

- `src/ui/edit/timeline_items.py`
- `src/ui/edit/timeline_view.py`
- `src/core/edit/audio_waveform.py`

Problem:

`ClipItem._build_real_waveform_path()` converts source frames to seconds:

```python
start_time = self._source_start / self._fps
end_time = (self._source_start + self._source_duration) / self._fps
```

If `_fps` is incorrectly defaulted to 24 for audio assets in a 30fps timeline, waveform is drawn from the wrong time range.

Required fix:

In `TimelineView._get_asset_fps(asset_id)`:

- If asset has valid fps, use it.
- If not, fall back to current timeline fps.
- Only fall back to 24 if no timeline is available.

### 5.2 Waveform Path Cache Must Include FPS

Problem:

Waveform path cache key may include source start/duration and width, but not fps. If the same clip data is shown under different fps assumptions, stale waveform paths can be reused.

Required fix:

In `ClipItem._draw_waveform()` cache key:

```python
cache_key = (
    f"{source_start}_{source_duration}_{display_width:.1f}"
    f"_fps_{fps:.4f}_stereo_{is_stereo}"
)
```

### 5.3 Async Failure Should Not Look Like Permanent Loading

Check:

- `WaveformCache.clear_pending(asset_id)` is called on success and failure.
- `ClipItem._waveform_requested` should not leave the item forever in loading if extraction fails.

Recommended improvement:

- Connect waveform failure signal if available.
- Or reset `_waveform_requested = False` when cache is no longer pending and no waveform exists, so repaint can retry or show placeholder.

Acceptance:

- Audio-only clips in 30fps timeline show waveform aligned to the same seconds as the ruler.
- Changing sequence fps or reloading timeline does not reuse stale waveform path.
- Failed waveform extraction shows placeholder, not permanent loading.

---

## 6. Minimum Test Plan

Use the project virtualenv:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/edit/test_timeline_view_layout.py \
  tests/edit/test_timeline_preview_renderer.py \
  tests/edit/test_audio_waveform.py \
  tests/edit/test_timeline_ops.py -q
```

Add / maintain tests:

### 6.1 Layout and Ruler Tests

File:

- `tests/edit/test_timeline_view_layout.py`

Required cases:

- Video and audio track y positions are cumulative.
- Audio clip y equals audio header y.
- Zoom does not change track y.
- Ruler hit test returns `-1`.
- A1 hit test returns correct index.
- Scene rect height includes ruler plus all real track heights.

### 6.2 Duration Mapping Tests

Required cases:

- 30fps / 300-frame / 10s asset in 24fps sequence:
  - `timeline_duration == 240`
  - `source_duration == 300`
  - `speed == 1.25`
- Audio asset without fps in 30fps sequence:
  - `_get_asset_fps(asset_id) == 30.0`

### 6.3 Renderer Stale Frame Tests

File:

- `tests/edit/test_timeline_preview_renderer.py`

Required cases:

- `set_sequence()` clears cache.
- `set_sequence()` clears pending frame.
- Old generation task does not emit `frame_ready`.
- Queue coalesces stale render requests during playback.

### 6.4 Manual UI Acceptance

Use a real project with:

- One 24fps timeline.
- One 30fps video clip with audio.
- One audio-only wav/mp3.

Checklist:

- Ruler visible on empty timeline.
- Dragging media over ruler does not create/drop clip on V1.
- Dropping video on V1 creates video clip and linked A1 clip with matching visual duration.
- Clip visual length equals seconds on ruler.
- Play from frame 0 shows image in Program Viewer, not blank.
- Seek to middle of clip shows image.
- Remove clip and add another clip; Program Viewer does not show previous clip or stale no-clip state.
- Audio waveform appears and aligns with clip boundaries.

---

## 7. Recommended Implementation Order

1. Add timeline layout helpers in `TimelineView`.
2. Route all track/header/clip/gap positioning through `_layout_track_items()`.
3. Fix ruler/grid x offset and scene rect duration/height.
4. Fix asset duration conversion: timeline frames vs source frames.
5. Fix `MainWindow` preview renderer refresh on `timeline_changed`.
6. Add renderer signal disconnect on renderer recreation.
7. Add renderer generation id checks to drop stale worker output.
8. Fix waveform fps fallback and cache key.
9. Add tests listed above.
10. Run manual UI acceptance with real media.

---

## 8. Watch-Outs

- Do not use system `python`; use `.venv/bin/python`.
- Do not make OpenGL calls from timeline UI code.
- Do not mutate `TimelineSequence` in place. Use existing immutable helpers / command pattern.
- Do not let `QGraphicsItem.ItemIsMovable` fight custom drag logic. Timeline clips should be positioned by the model/view layout code.
- Do not treat source frame count as timeline frame count unless asset fps equals timeline fps.
- Do not keep old Qt signal connections when replacing renderer instances.

---

## 9. Definition Of Done

This repair is done only when all of the following are true:

- Ruler is visible and aligned with clip frame positions.
- Clip visual duration matches real seconds on the timeline.
- Program Viewer updates immediately after clip insert/delete/move/trim.
- Playback no longer blanks because of stale empty-sequence render results.
- Waveform time range matches clip source range and sequence fps assumptions.
- Automated tests pass.
- Manual UI checklist passes with real video and audio media.
