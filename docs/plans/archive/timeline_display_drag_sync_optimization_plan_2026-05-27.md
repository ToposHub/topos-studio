# Timeline Display, Drag, and Media Sync Optimization Plan

Date: 2026-05-27

Scope: Edit page timeline display and interaction stability.

Primary user-visible goals:

- Timeline tracks, clips, thumbnails, waveform, ruler, and playhead must visually align.
- Dragging clips must feel solid: no ghosting, no residual image, no delayed linked audio/video movement.
- Dragging imported media into the timeline must use the same coordinate system as the final dropped clips.
- Video clips with linked audio must move together during preview and after commit.
- The yellow line in the V1 header must either be clearly communicated as the target-track indicator or restyled so it is not mistaken for a layout bug.

This plan is based on the screenshot from `/Users/testuser/Desktop/截屏2026-05-27 03.24.37.png` and the current implementation in:

- `src/features/edit/ui/timeline_view.py`
- `src/features/edit/ui/timeline_items.py`
- `src/features/edit/core/timeline_v2.py`
- `src/features/edit/core/audio_waveform.py`
- `src/shared/media/thumbnail_provider.py`
- `tests/edit/test_timeline_view_layout.py`

The plan intentionally does not modify core rendering modules such as `async_renderer.py`, `shader_compiler.py`, or `gl_backend.py`.

---

## 1. Current Symptoms From Screenshot

### 1.1 Track/clip alignment is visibly unreliable

Observed:

- The clip appears between V2 and A3-like visual rows, while the header labels and item body do not feel locked to a single track.
- V1, V2, A3, and A4 headers exist, but the dropped material appears not to correspond cleanly to the intended video/audio track pairing.
- Video and audio clip bodies look horizontally linked but vertically uncertain.

Likely causes:

- `TimelineView._layout_track_items()` now calculates cumulative track y positions correctly.
- `ClipItem.paint()` still forcibly recalculates y during painting with:
  - `TRACK_AREA_TOP + self._track_index * (self._track_height + TRACK_GAP)`
- This only works when every track above the clip has the same height as the current clip. It fails for mixed video/audio heights and custom track heights.
- `DragPreviewItem._update_geometry()` and `_draw_snap_indicator()` also use fixed `TRACK_HEIGHT + TRACK_GAP`, not cumulative track tops.

Required correction:

- `TimelineView` must be the only owner of scene x/y placement for track-bound items.
- `ClipItem.paint()` must never call `setPos()`.
- Item classes may calculate their local `boundingRect()` and internal visual content, but scene position must come from `TimelineView`.

### 1.2 Dragging leaves ghosting/residual visuals

Observed:

- During dragging, old item positions can remain briefly visible.
- The item appears to be repainted from more than one source of truth.

Likely causes:

- Geometry changes sometimes happen without `prepareGeometryChange()`.
- `ClipItem.paint()` mutates scene position during painting, causing invalidation during paint.
- Live drag updates call `_update_geometry()` repeatedly, while the model still has the old `timeline_start`.
- Linked clips update only partly during preview and more fully on release.
- `QGraphicsView.FullViewportUpdate` helps but cannot fully hide geometry mutations that occur inside `paint()`.

Required correction:

- No geometry mutation in `paint()`.
- Every width/height/bounding-rect-affecting change calls `prepareGeometryChange()` before assignment.
- Live drag uses a single preview state and invalidates old/new rects explicitly.
- Final command commit happens once per interaction.

### 1.3 V1 header yellow line is probably a target-track indicator

Observed:

- V1 header has a vertical yellow/orange line at the right edge.

Current implementation:

- `TrackHeaderItem.paint()` draws a target track indicator when `_is_target` is true.
- `TimelineView.load_timeline()` sets the first video track as target by default.
- The indicator color is `TARGET_TRACK_INDICATOR_COLOR = QColor(217, 130, 43)`.
- `_draw_target_indicator()` draws a vertical right-edge line and small triangle.

Conclusion:

- This is likely not a random rendering line. It is the active target track indicator.
- The problem is visual semantics: in the current UI it looks like a broken vertical separator or accidental yellow line.

Required correction:

- Keep the feature, but restyle it:
  - Use a subtle left-edge accent or a compact target icon instead of a full-height right-edge line.
  - Add hover tooltip on the track header: "Target track".
  - Ensure only one video target and one audio target can be active.

### 1.4 Video and audio can be linked, but live dragging does not feel synchronized

Observed:

- Video and timeline can move together after drop/commit, but dragging feels out of sync.

Current implementation:

- `_add_asset_as_clip()` creates linked video/audio clip IDs.
- `_on_drag_finished()` moves the linked clip after the primary clip has finished moving.
- `_on_clip_moved()` only updates the actively dragged item during live preview.
- Linked item visual update is mostly delayed until release.

Required correction:

- During live drag, calculate one shared `delta_frames`.
- Apply the same delta to:
  - actively dragged clip,
  - linked audio/video mate,
  - selected clips in the same move group.
- Validate the entire group before showing a valid move state.
- Commit the group move as a single macro command.

### 1.5 Timeline thumbnails have incorrect aspect/placement

Observed:

- Timeline preview thumbnails look squeezed/cropped/incorrect for their strip.

Current implementation:

- `ClipThumbnailWorker` extracts thumbnails scaled to a fixed height while preserving source aspect.
- `ClipItem._draw_cached_thumbnails()` draws each pixmap using a cover-style `KeepAspectRatioByExpanding` approach and clips overflow.
- That is acceptable for a filmstrip if the cell geometry is stable, but the current cell count and x position are derived from item width, not exact source/time mapping.
- There is an unused `frame_offset` variable, suggesting frame-accurate positioning was intended but not completed.

Required correction:

- Decide one explicit timeline thumbnail mode:
  - `cover`: fill the strip cell, crop overflow, no distortion.
  - `contain`: preserve full frame with letterbox/pillarbox, no crop.
- For editing timelines, recommended default is `cover` for compact filmstrip readability, with no stretching.
- Draw each thumbnail at the x position corresponding to its source frame relative to the clip's source range, not just `i * width / count`.
- Add a debug/test mode to verify source aspect is preserved.

### 1.6 Audio waveform appears abnormal

Observed:

- The waveform appears like tiny repeated blocks or nearly flat/noisy artifacts instead of readable peaks.

Current implementation risks:

- `ClipItem._build_real_waveform_path()` maps source frame range to seconds with:
  - `start_time = self._source_start / self._fps`
  - `end_time = (self._source_start + self._source_duration) / self._fps`
- For audio clips or video clips with audio, `_fps` may be timeline fps or media video fps, not audio sample timing.
- `WaveformData` stores duration in seconds and samples per second, so waveform display should ideally derive time range from clip time mapping, not assume source video frames equal audio timeline frames.
- `_request_waveform_async()` currently has a guard that can treat "not pending and no cache" as a previous failure before any real request, which risks never requesting waveforms.

Required correction:

- Define a single waveform time mapping:
  - timeline clip frame range maps to timeline seconds using sequence fps;
  - source audio offset maps to source seconds using source fps only when source_start/source_duration are stored in source video frames;
  - for audio-only assets, source_start/source_duration must be interpreted with a documented source timebase.
- Store or resolve asset waveform duration and source fps/sample rate separately.
- Ensure waveform extraction is requested once and retry/failure state is explicit, not inferred from "no cache".

---

## 2. Design Principle: One Timeline Geometry Contract

### 2.1 Coordinate ownership

Only `TimelineView` should know the global scene layout:

- frame to scene x
- scene x to frame
- track index to scene y
- scene y to track index
- track height
- total track area height
- header width offset
- ruler height offset

Item classes should know only:

- their local width and height,
- their local drawing rect,
- their local handles,
- their local text/icons,
- signals for requested operations.

### 2.2 Required coordinate API

Keep or formalize these helpers in `TimelineView`:

```python
def _ordered_tracks(self) -> list[EditTrack]: ...
def _default_track_height(self, track: EditTrack) -> int: ...
def _track_height_for(self, track: EditTrack) -> int: ...
def _track_top_for_index(self, target_index: int) -> float: ...
def _track_index_at_y(self, scene_y: float) -> int: ...
def _total_track_area_height(self) -> float: ...
def _frame_to_x(self, frame: int) -> float: ...
def _x_to_frame(self, scene_x: float) -> int: ...
def _clip_scene_rect(self, track_index: int, start: int, duration: int) -> QRectF: ...
```

Then replace direct coordinate math in items with calls from `TimelineView`.

### 2.3 Files to adjust

Primary:

- `src/features/edit/ui/timeline_view.py`
- `src/features/edit/ui/timeline_items.py`

Secondary:

- `tests/edit/test_timeline_view_layout.py`

Acceptance:

- Audio clip y remains correct after zoom.
- Audio clip y remains correct after track height changes.
- Drag preview y matches final dropped clip y.
- No item calls `setPos()` from `paint()`.
- No track-bound item uses fixed `TRACK_HEIGHT` for scene y when mixed track heights are possible.

---

## 3. Phase 1: Fix Timeline Display Alignment First

Priority: Critical.

Goal: Make static timeline display correct before addressing complex drag behavior.

### 3.1 Remove scene-position mutation from `ClipItem.paint()`

Current risk:

- `ClipItem.paint()` calculates `expected_x` and `expected_y`.
- If position differs, it calls `self.setPos(expected_x, expected_y)`.
- This is unsafe because painting must be a pure draw operation.

Change:

- Delete the `setPos()` correction from `paint()`.
- Keep optional debug assertion/logging only behind a disabled debug flag if needed.

Suggested replacement:

```python
if TIMELINE_GEOMETRY_DEBUG:
    expected = self.data(GeometryDebugRole)
    # log only, never mutate geometry from paint
```

Acceptance:

- Dragging no longer triggers position correction during paint.
- Static timeline still positions correctly through `_layout_track_items()`.

### 3.2 Make `ClipItem._update_geometry()` update width only

Current risk:

- `ClipItem._update_geometry()` updates x/y and width using `TimelineCoordinateManager`.
- But the view also positions the item using cumulative track layout.

Change:

- Rename or split:
  - `_update_visual_width()`
  - `_set_layout_geometry(width, track_height)` if needed
- `ClipItem` should not set scene y internally.
- For live drag, `TimelineView` should call a method like:

```python
item.set_timeline_visual_state(
    start_frame=new_start,
    duration_frames=item.duration_frames,
    pixels_per_frame=self._pixels_per_frame,
    track_height=self._track_height_for(track),
)
self._position_clip_item(item, track_index)
```

Acceptance:

- `ClipItem` local width changes correctly after zoom/trim.
- Scene x/y are applied once by `TimelineView`.

### 3.3 Add one layout method for all clip-like items

Implement:

```python
def _position_clip_item(self, item: ClipItem, track_index: int, start_frame: int) -> None:
    item.set_track_height(self._track_height_for(self._ordered_tracks()[track_index]))
    item.update_track_index(track_index)
    item.setPos(self._frame_to_x(start_frame), self._track_top_for_index(track_index))
```

Use it from:

- `load_timeline()`
- `set_zoom()`
- `_on_clip_moved()`
- `_on_clip_trim_left()`
- `_on_clip_trim_right()`
- incremental update paths
- track height change paths

Acceptance:

- All clip y values equal their corresponding header y values.
- Clip x at frame 0 equals `TRACK_HEADER_WIDTH`.

### 3.4 Make drag preview use cumulative track y

Current risk:

- `TimelineView._update_drag_preview()` already sets the preview position with `_track_top_for_index()`.
- But `DragPreviewItem.show_preview()` calls `set_position()`, which calls `_update_geometry()`, which uses fixed `TRACK_HEIGHT`.
- The view then calls `setPos()` again. This duplicated positioning can create flicker.

Change:

- Make `DragPreviewItem` local-only:
  - `set_position()` should store `_track_index` and `_frame` but not call `setPos()`, or accept an explicit scene position from the view.
- Add:

```python
def set_layout_metrics(self, track_height: int, track_top_offset: float, track_tops: list[float]) -> None: ...
```

or simpler:

```python
def show_preview(..., track_height: int) -> None: ...
```

Then `TimelineView` remains responsible for:

```python
self._drag_preview_item.setPos(self._frame_to_x(frame), self._track_top_for_index(track_index))
```

Acceptance:

- Drag preview appears on the exact same y as the final dropped clip.
- Drag preview does not jump between fixed and cumulative y during drag.

---

## 4. Phase 2: Eliminate Drag Ghosting and Residual Paint

Priority: Critical.

Goal: Movement should look like one solid object being dragged.

### 4.1 Enforce `prepareGeometryChange()` before geometry changes

Audit these methods in `ClipItem`:

- `_update_geometry()`
- `update_zoom()`
- `update_duration()`
- `update_track_index()`
- `set_track_height()`
- `update_clip_properties()`
- trim/slide/ripple preview methods

Rule:

- If `_width`, `_track_height`, or anything affecting `boundingRect()` changes, call `prepareGeometryChange()` first.
- If only color/text/hover state changes, call `update()` only.

Acceptance:

- No residual old bounding rect remains after trim or drag.
- Selected border and hover border do not leave stale pixels.

### 4.2 Avoid double movement during drag

Current design:

- `ClipItem` handles mouse movement and emits `clip_moved`.
- `TimelineView._on_clip_moved()` updates item geometry.
- `ClipItem.mouseMoveEvent()` calls `super().mouseMoveEvent(event)` at the end.
- The item comments say `ItemIsMovable` was avoided, but the superclass call can still participate in event propagation and repaints.

Change:

- For active timeline edit modes, consume the mouse event after emitting the signal:

```python
event.accept()
return
```

- Do not call `super().mouseMoveEvent(event)` while `_is_dragging` is true.

Acceptance:

- The item moves only through `TimelineView`.
- Drag no longer produces competing preview positions.

### 4.3 Separate live preview state from committed model state

Current risk:

- During `_on_clip_moved()`, `item._start_frame` is set to `new_start`.
- The actual timeline model still has old `timeline_start` until `MoveClipCommand` executes.
- Any code that mixes item state and model state during drag can create mismatch.

Change:

- Introduce item preview fields:
  - `_preview_start_frame: Optional[int]`
  - `_preview_duration_frames: Optional[int]`
- Drawing uses preview value when present.
- On release:
  - commit command,
  - clear preview after model reload/incremental update.
- On cancel/invalid:
  - clear preview and relayout from model.

Simpler first step:

- Keep current `_start_frame` mutation, but centralize it through `TimelineView._set_clip_preview_position()`.
- Ensure linked clips and selected clips use the same method.

Acceptance:

- Drag preview and final commit never disagree by one frame.
- Invalid move reverts every moved item together.

### 4.4 Use viewport update mode deliberately

Current state:

- `TimelineView` uses `QOpenGLWidget` viewport and `FullViewportUpdate`.

Potential issue:

- Full viewport redraw is safe but expensive.
- With geometry mutation inside `paint()`, it can still show flicker.

After fixing geometry purity:

- Keep `FullViewportUpdate` initially to verify no ghosting.
- Later benchmark `BoundingRectViewportUpdate` for performance.

Acceptance:

- With `FullViewportUpdate`, no ghosting during drag.
- If switching to bounding updates, ghosting must still be absent.

---

## 5. Phase 3: Linked Video/Audio Drag Sync

Priority: Critical.

Goal: Linked video/audio clips must move as one object visually and logically.

### 5.1 Define a move group

Add helper in `TimelineView`:

```python
def _build_move_group(self, primary_clip_id: str) -> list[str]:
    ...
```

Group should include:

- primary dragged clip,
- linked clip if present,
- selected clips if multi-selection is active,
- linked mates of selected clips.

Return stable de-duplicated clip IDs.

Acceptance:

- Dragging video clip previews linked audio movement immediately.
- Dragging audio clip previews linked video movement immediately.
- Multi-selection with linked clips does not double-move any clip.

### 5.2 Validate the whole group before applying preview

Add:

```python
def _validate_move_group(self, move_group: list[str], delta_frames: int) -> bool:
    ...
```

Rules:

- No clip may move before frame 0.
- No clip may overlap clips outside the move group on its track.
- Locked tracks reject movement.
- Track type does not change unless explicit vertical dragging is implemented later.

Acceptance:

- If linked audio would collide, both video and audio show invalid state or revert.
- No partial valid/invalid mismatch.

### 5.3 Apply live preview to the whole group

Modify `_on_clip_moved()`:

- Compute `delta_frames = new_start - original_start`.
- Snap based on the primary clip's requested edge.
- Apply the same snapped delta to every group item.
- Update all involved items before repaint.

Acceptance:

- Video/audio linked items remain horizontally locked during drag.
- The final release position matches the live preview.

### 5.4 Commit as a macro command

Current risk:

- Primary clip and linked clip are moved as separate commands.
- Undo/redo may require multiple steps or create intermediate inconsistent states.

Change:

- Use `MacroCommand` or add a dedicated `MoveClipGroupCommand`.
- One drag release creates one undo step.

Acceptance:

- One undo returns both video and audio to the original start.
- One redo moves both again.

---

## 6. Phase 4: Fix Target Track Indicator UX

Priority: High.

Goal: The V1 yellow line should stop looking like a bug.

### 6.1 Keep semantics, change visual style

Current:

- Right-edge full-height orange line and triangle.

Recommended:

- Move indicator to left edge inside the header.
- Use a narrow 3px accent plus subtle filled header tint.
- Add a small target icon or "V" / "A" active badge if icons are already available.

Implementation:

- Update `TrackHeaderItem._draw_target_indicator()`.
- Keep `TARGET_TRACK_INDICATOR_COLOR`, but reduce visual dominance.

Acceptance:

- Active target is visible.
- It does not look like a broken separator between header and timeline.

### 6.2 Enforce one target per media type

Current:

- `_on_target_track_changed()` should clear old target, but verify both video/audio behavior.

Expected behavior:

- One video target for video insert/overwrite.
- One audio target for audio insert/overwrite.
- Dropping a video with audio should use video target plus audio target, or first available audio target.

Acceptance:

- Setting V2 target clears V1 target.
- Setting A1 target does not clear video target unless the implementation intentionally supports only one global target.

---

## 7. Phase 5: Thumbnail Aspect and Filmstrip Accuracy

Priority: High.

Goal: Timeline thumbnails must preserve source aspect and map to the correct clip time.

### 7.1 Add explicit thumbnail fit mode

In `ClipItem`:

```python
THUMBNAIL_FIT_MODE = "cover"  # allowed: "cover", "contain"
```

For `cover`:

- Scale pixmap preserving aspect ratio.
- Fill target cell.
- Center crop overflow.
- Never stretch.

For `contain`:

- Scale preserving aspect ratio.
- Fit whole image inside target cell.
- Fill letterbox/pillarbox with clip background.
- Never stretch.

Acceptance:

- A 16:9 source never becomes square-stretched.
- A vertical source never becomes horizontally distorted.

### 7.2 Position thumbnails by source frame offset

Current:

- `frame_offset = frame_idx - self._source_start` is computed but unused.
- x position uses thumbnail sequence index.

Change:

- Use:

```python
relative = (frame_idx - self._source_start) / max(1, self._source_duration)
x_pos = inner_rect.left() + relative * inner_rect.width()
```

- Thumbnail cell width can still use spacing, but position should come from source time.

Acceptance:

- Trimmed clips show thumbnails from the trimmed source range.
- Speed-mapped clips display frames corresponding to the source range.

### 7.3 Make thumbnail request key include fit-affecting dimensions

Current:

- Cache key includes asset, source range, ppf, and clip width.
- It does not include thumbnail height explicitly.

Change:

- Include `thumbnail_height`.
- Include media rotation/aspect metadata if available.

Acceptance:

- Resizing track height invalidates old thumbnail strip sets.
- Rotated media does not reuse wrong thumbnail geometry.

---

## 8. Phase 6: Waveform Accuracy

Priority: High.

Goal: Waveforms should be readable, stable, and mapped to the correct part of the audio.

### 8.1 Fix waveform request lifecycle

Current risk:

- `_request_waveform_async()` may return early when no cache exists and not pending.
- That condition can happen before the first request.

Change:

- Add explicit failed-state tracking:

```python
self._waveform_failed: bool = False
```

- Request when:
  - asset id exists,
  - asset path exists,
  - not requested,
  - not pending,
  - not failed.

Acceptance:

- A newly loaded audio clip requests waveform extraction on first paint.
- Failed extraction does not retry every paint.
- Manual cache clear or asset reload can reset failure state.

### 8.2 Define source timebase for audio clips

Problem:

- `source_start/source_duration` are frame fields, but audio needs seconds/sample mapping.

Short-term fix:

- For linked video audio:
  - source frames use video fps.
  - source seconds = source_frame / asset_video_fps.
- For audio-only files:
  - source frames should be timeline-frame domain unless a separate audio source frame rate is introduced.
  - source seconds = source_frame / timeline_fps.

Better long-term fix:

- Add source timebase metadata to `ClipInstance` or a helper resolver:

```python
def _clip_source_time_range_seconds(self, clip: ClipInstance, asset: MediaAsset) -> tuple[float, float]:
    ...
```

Acceptance:

- 10 second audio asset shows 10 seconds of waveform.
- Trimming the first 2 seconds starts waveform at 2 seconds.
- Linked video/audio waveforms remain aligned with video thumbnails.

### 8.3 Improve waveform drawing density

Current:

- Path uses one point per display pixel.
- Filled envelope can look like blocks at very small heights.

Change:

- Clamp waveform area:
  - minimum top/bottom margins,
  - if track height too small, draw center-line waveform only.
- Use vertical peak bars at low zoom if path becomes visually noisy.
- Cache by:
  - asset id,
  - source time range,
  - display width,
  - channel mode,
  - track height.

Acceptance:

- Waveform remains readable at default zoom.
- Stereo display has two distinct lanes.
- No random placeholder waveform is shown after real waveform is available.

---

## 9. Phase 7: Ruler, Playhead, and Timeline Pan/Scroll Sync

Priority: Medium to High.

Goal: Moving the timeline viewport, playhead, and viewer preview should feel synchronized.

### 9.1 Keep x mapping identical everywhere

Required invariant:

```text
scene_x = TRACK_HEADER_WIDTH + frame * pixels_per_frame
frame = round((scene_x - TRACK_HEADER_WIDTH) / pixels_per_frame)
```

Apply to:

- ruler ticks,
- playhead,
- clip x,
- drag preview x,
- snap indicator x,
- gap indicator x,
- marker x,
- background grid.

Acceptance:

- Ruler tick at `00:00` aligns with clip start at frame 0.
- Playhead line aligns with the frame under the mouse.

### 9.2 Viewer preview should update from the same frame source

Current concern:

- User reports video and timeline "can drag together but feel not synchronized".
- Part of this may be linked clip movement; part may be viewer frame requests during scrubbing.

Change:

- During playhead drag:
  - update playhead visual immediately,
  - emit `playhead_changed(frame)`,
  - throttle expensive preview rendering to a stable interval if needed.
- During clip drag:
  - do not scrub viewer unless explicitly intended.
  - If preview-on-drag is enabled, render based on the prospective timeline frame with a debounce.

Acceptance:

- Scrubbing feels immediate.
- Clip dragging does not trigger stale viewer frames that make the UI feel out of sync.

---

## 10. Phase 8: Tests and Verification

Priority: Critical for preventing regressions.

### 10.1 Unit tests for geometry

Extend `tests/edit/test_timeline_view_layout.py`.

Add tests:

- `test_clip_item_paint_does_not_change_scene_position`
- `test_drag_preview_uses_cumulative_track_top`
- `test_track_height_change_repositions_audio_clip`
- `test_mixed_video_audio_track_heights_keep_items_aligned`
- `test_frame_zero_x_equals_track_header_width`

Acceptance:

- Tests fail before geometry cleanup and pass after.

### 10.2 Unit tests for linked movement

Add tests:

- `test_linked_clip_live_preview_moves_both_items`
- `test_linked_clip_move_rejects_when_audio_collides`
- `test_linked_clip_drag_commits_single_macro_command`
- `test_undo_redo_group_move_restores_both_clips`

Acceptance:

- Moving video updates audio live.
- Moving audio updates video live.
- Undo is one step.

### 10.3 Thumbnail tests

Add tests around pure geometry helpers rather than requiring real video decode:

- `test_thumbnail_cover_rect_preserves_aspect`
- `test_thumbnail_contain_rect_preserves_aspect`
- `test_thumbnail_x_position_uses_source_frame_offset`
- `test_thumbnail_cache_key_changes_with_track_height`

Acceptance:

- No thumbnail helper returns a stretched target rect.

### 10.4 Waveform tests

Add tests:

- `test_waveform_request_starts_when_no_cache_and_not_pending`
- `test_waveform_time_range_for_linked_video_audio_uses_asset_fps`
- `test_waveform_time_range_for_audio_only_uses_timeline_fps_or_audio_timebase`
- `test_waveform_cache_key_changes_with_track_height`

Acceptance:

- First paint requests waveform.
- Trimmed source range maps to expected seconds.

### 10.5 Visual/manual verification checklist

Use a test project with:

- one 16:9 video with audio,
- one vertical video with audio,
- one audio-only wav/mp3,
- one still image,
- mixed track heights,
- at least two video tracks and two audio tracks.

Manual checks:

- Drop 16:9 video on V1: video appears on V1, audio appears on target/first audio track.
- Drag linked video: audio moves in the same frame with no lag.
- Drag linked audio: video moves in the same frame with no lag.
- Drag over an invalid overlap: whole group rejects or shows invalid, no partial move.
- Zoom in/out: y positions do not change.
- Resize audio track: waveform stays inside audio track.
- Ruler tick lines align with clip edges.
- No ghosting after 20 quick drags.
- V1 target indicator is understandable and not mistaken for a bug.

---

## 11. Recommended Implementation Order

### Step 1: Geometry purity

Files:

- `src/features/edit/ui/timeline_items.py`
- `src/features/edit/ui/timeline_view.py`
- `tests/edit/test_timeline_view_layout.py`

Tasks:

- Remove `setPos()` from `ClipItem.paint()`.
- Stop `ClipItem._update_geometry()` from owning scene y.
- Add central positioning helpers in `TimelineView`.
- Fix drag preview y ownership.

Expected result:

- Static timeline display becomes normal.
- Drag preview lands on correct track.

### Step 2: Ghosting cleanup

Files:

- `src/features/edit/ui/timeline_items.py`
- `src/features/edit/ui/timeline_view.py`

Tasks:

- Audit `prepareGeometryChange()`.
- Consume mouse move events during active item dragging.
- Centralize live preview update.

Expected result:

- Moving clips leaves no residual visuals.

### Step 3: Linked video/audio movement

Files:

- `src/features/edit/ui/timeline_view.py`
- `src/features/edit/core/commands.py` if a new group command is needed
- `tests/edit/test_timeline_view_layout.py` or a new `test_timeline_view_drag.py`

Tasks:

- Build move group.
- Validate group.
- Preview group.
- Commit group as macro command.

Expected result:

- Video and audio feel like one linked object.

### Step 4: Target track indicator restyle

Files:

- `src/features/edit/ui/timeline_items.py`
- potentially `src/features/edit/ui/edit_style.py`

Tasks:

- Restyle active target indicator.
- Add tooltip/hover label if existing UI pattern supports it.

Expected result:

- Yellow V1 mark becomes understandable.

### Step 5: Thumbnail correction

Files:

- `src/features/edit/ui/timeline_items.py`
- `src/shared/media/thumbnail_provider.py`
- tests for thumbnail geometry

Tasks:

- Add fit mode helper.
- Use source frame offset for x placement.
- Include height/aspect in cache key.

Expected result:

- Timeline thumbnails preserve aspect ratio and source range.

### Step 6: Waveform correction

Files:

- `src/features/edit/ui/timeline_items.py`
- `src/features/edit/core/audio_waveform.py`
- possible helper in `timeline_view.py` for asset timebase resolution

Tasks:

- Fix request lifecycle.
- Add explicit time range resolver.
- Improve waveform cache key and drawing density.

Expected result:

- Waveforms show real audio shape and align with clip duration.

---

## 12. Risk Notes

### 12.1 Avoid large rewrites

The current timeline is already feature-rich:

- snapping,
- trim,
- slip,
- slide,
- ripple trim,
- transitions,
- markers,
- gaps,
- target tracks,
- linked clips,
- thumbnails,
- waveform.

Do not rewrite the whole timeline in one pass. The safest route is to first enforce the geometry contract and then fix each behavior against that contract.

### 12.2 Protect user changes

The current worktree already has modifications in:

- `src/app/main_window.py`
- `src/features/edit/ui/timeline_items.py`
- `src/features/edit/ui/timeline_view.py`
- `tools/timeline_preview.py`

Implementation should inspect those changes before editing and avoid reverting unrelated work.

### 12.3 Do not touch GPU render pipeline for this pass

These bugs are primarily timeline UI/model-view synchronization issues. Avoid editing:

- `src/core/async_renderer.py`
- `src/core/shader_compiler.py`
- `src/core/gl_backend.py`

unless later testing proves viewer rendering itself is stale.

---

## 13. Definition of Done

The timeline repair is done when all of these are true:

- A clip at frame 0 starts exactly after the track header.
- Ruler tick at 0 aligns with playhead and clip start.
- Video and audio tracks have correct y positions under mixed heights.
- Dragging a clip shows one moving visual, not an old and new copy.
- Dragging linked video/audio moves both during live preview.
- Dropping a video with audio creates aligned linked video/audio clips.
- Target track indicator is clear and no longer looks like accidental yellow artifact.
- Timeline thumbnails preserve aspect ratio.
- Audio waveform is based on real extracted data when available.
- No waveform placeholder remains after successful extraction.
- Unit tests cover geometry, drag preview, linked movement, thumbnail rects, and waveform request lifecycle.

Recommended first implementation PR title:

`Fix timeline geometry ownership and drag preview alignment`

Recommended second implementation PR title:

`Make linked timeline clips move as one group`

Recommended third implementation PR title:

`Repair timeline thumbnails and waveform time mapping`
