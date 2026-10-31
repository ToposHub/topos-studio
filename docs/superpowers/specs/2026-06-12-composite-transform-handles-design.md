# Composite Transform Node — On-Viewer Control Handles (Fusion-style Upgrade)

- **Date:** 2026-06-12
- **Scope:** Composite module · `Transform` node only
- **Target:** Fusion-style on-viewer transform handles — move / rotate / scale / stretch — that appear automatically when a Transform node is selected and behave correctly under any rotation.
- **Reference:** Fusion Transform node on-screen control (user-provided screenshots). Drawing conventions borrowed from the color-page Crop tool and Masks Rectangle tool (atan2 rotation, cosmetic constant-size handles), adapted to the composite viewer's OpenGL overlay.

---

## 1. Background & Current State

The composite viewer (`CompositeViewer`, `src/features/composite/ui/composite_viewport.py`) is a `QOpenGLWidget`. It **already ships a complete transform-handle layer** — this is an upgrade, not greenfield:

| Capability | Status | Location |
|---|---|---|
| Param model (`translate_x/y`, `scale_x/y`, `rotation`, `pivot_mode/x/y`, `filter`) | ✅ works | `nodes/transform_node.py` (`TransformNode.PARAM_DEFS`, `_build_transform_matrix`) |
| GPU apply (`p' = R·S·(p−piv)+piv+t`; order scale→rotate, about pivot) | ✅ works | `evaluators/transform_evaluator.py` |
| 12 handle NDC positions + bbox | ⚠️ computed, **AABB geometry wrong under rotation** | `ui/viewer_handles.py` `compute_transform_handles` |
| OpenGL drawing (bbox + center→rotate line + circle/cross handles) | ⚠️ draws, **visually rough, frame is AABB** | `composite_viewport.py` `_draw_transform_auxiliary`, `_draw_handles_from_dict` |
| Drag → param computation | ⚠️ translate/rotate work; **scale is dead** (see §4) | `viewer_handles.py` `compute_transform_delta_params`; `composite_page.py` `_compute_new_params` |
| Shift (aspect lock) / Ctrl (axis lock) constraints + snapping | ✅ works | `composite_viewport.py` `_handle_transform_move`; `viewer_constraints.py` |
| Undo/redo single-commit | ✅ works | `interaction_ended` signal → `composite_page.py` |

**Transform node transform (from `_build_transform_matrix`):**

```
linear part = R(θ)·S(sx,sy),  where R=[[cos,-sin],[sin,cos]], S=diag(sx,sy)
full:        p' = R·S·(p − piv) + piv + t
```

i.e. scale is applied first (in local space), then rotate about the pivot, then translate by `t = (translate_x, translate_y)`. Pivot is resolved to image center when `pivot_mode == "center"`.

### Problems to fix

- **P1 — Not auto-shown.** Handles only draw when `_tool_mode == TRANSFORM`. Default mode is `PAN`, and selecting a node does not switch the mode (`set_selected_node` calls `_update_tool_handles` but not `set_tool_mode`). So users see nothing unless they press the tool shortcut. Confirmed by the user's own report ("需要在视图窗口上显示控制手柄").
- **P2 — Geometry wrong under rotation.** `compute_transform_handles` computes the 4 transformed corners then takes axis-aligned `min/max` as `bbox_rect`; edge handles are placed on that AABB; the rotate stalk is a fixed NDC offset above the AABB. Under any non-zero rotation the frame, edge handles, and stalk detach from the actual rotated image rectangle.
- **P3 — Scale math not rotation-aware.** `compute_transform_delta_params` computes scale deltas from raw screen `dx/dy` without un-rotating by the current rotation, and uses heuristic opposite-edge translate compensation that is only correct at rotation 0 / pivot center. Scaling a rotated transform drifts.
- **P4 — Scale handles are dead code (hidden bug).** In `_handle_transform_move` (and `_handle_transform_press`) the handle-type classifier is:
  ```python
  handle_type = handle.split("_")[0] if "_" in handle else handle
  is_scale = handle_type in ("top","bottom","left","right","corner","edge")
  ```
  Our handle IDs are `scale_corner_br`, `scale_x_left`, etc. → `split("_")[0] == "scale"` → **never in the tuple** → `is_scale` always `False` → `_compute_new_params` never enters the scale branch → **dragging corners/edges changes no parameter.** Translate and rotate are unaffected.

---

## 2. Requirements (confirmed with user)

1. **Activation:** selecting a `Transform` node shows handles automatically (no tool-mode switch, no shortcut). Deselecting / selecting a non-Transform node hides them.
2. **Handle set (Fusion full):** center (move) · 4 corners (scale, Shift = uniform) · 4 edges (single-axis stretch) · rotate stalk (attached to rotated top edge) · draggable pivot point.
3. **Scope:** `Transform` node only. CornerPin / Mask / Roto left untouched (they share the same flawed rotation pattern but are out of scope).

---

## 3. Design

### 3.1 Activation — decouple drawing from tool mode + handle-hit priority

Two contained changes in `composite_viewport.py`:

- **Drawing gate.** In `_draw_tool_handles`, replace the early-return on `PAN/SELECT` with: draw the selected node's handles whenever `_interaction_state.selected_node_id` is set **and** `_selected_node_type` is a transformable type (start with `"transform"`). Tool mode no longer gates visibility for the selected-node overlay.
- **Hit priority in `mousePressEvent`.** Before dispatching to the current tool, run `hit_test_handle_at_pos(view_pos)`. If a handle of the selected Transform node is hit, start a drag with `tool=TRANSFORM` via the existing `_handle_transform_press` path (which seeds `_interaction_state`). Because `mouseMoveEvent`/`mouseReleaseEvent` dispatch on `_interaction_state.tool` once dragging, the entire move/release/constraint/snap/commit pipeline is reused unchanged. If no handle is hit, fall through to the current tool's press handler.

The dedicated `TRANSFORM` tool mode and its keyboard shortcut remain for compatibility; they are simply no longer required to see or grab handles.

### 3.2 Geometry — rotation-aware frame & handle placement (`viewer_handles.py`)

Rewrite `compute_transform_handles` to keep the **ordered** transformed corners and derive everything from them, instead of an AABB.

Let the original image corners (image coords, y-down) be:
```
TL=(0,0)  TR=(W,0)  BR=(W,H)  BL=(0,H)
```
Transform each by `f(p) = R·S·(p − piv) + piv + t` (pivot resolved per `pivot_mode`). Keep them in ordered form `TL', TR', BR', BL'`.

- **Frame** = line-loop quad through `TL', TR', BR', BL'` (the rotated rectangle outline). Replaces `bbox_rect` (AABB). `TransformHandlesData` gains `frame_corners: tuple[4]` (ordered) and keeps `bbox_rect` only as the screen-AABB for hit-extension if needed.
- **Corner handles** = `TL', TR', BR', BL'`.
- **Edge handles** = midpoints of each frame edge:
  - top = `(TL'+TR')/2`, bottom = `(BL'+BR')/2`, left = `(TL'+BL')/2`, right = `(TR'+BR')/2`.
- **Local axes** (unit vectors in screen/NDC space along the rotated image's axes; used only to detect flip direction and to sanity-check stalk orientation — the frame quad itself is drawn directly from the corner positions):
  - `axis_x = normalize(TR' − TL')` (image +X direction)
  - `axis_y = normalize(BL' − TL')` (image +Y direction)
  - If a scale sign is negative (flip), invert the corresponding axis.
- **Rotate stalk** attached to the rotated top edge:
  - `stalk_base = top_edge_midpoint`
  - `stalk_dir = normalize(top_mid − bottom_mid)` (points "up", toward image y=0 side)
  - `rotate_handle = stalk_base + stalk_dir · stalk_length`, where `stalk_length` is a **constant screen-pixel** offset (converted to NDC via current zoom), so the stalk stays a fixed visual length regardless of zoom.
- **Center handle** = transformed image center = `f((W/2, H/2))`.
- **Pivot handle** = transformed pivot = `f(piv)` = `piv + t` (since `f(piv)=piv+t`). Shown always; dragging it switches `pivot_mode` to `"custom"` (see 3.3).

### 3.3 Drag math — local-space, anchor-fixed (`compute_transform_delta_params`)

Work in the **transformed pivot** frame: `pivot_world = piv + t` (the world location of the pivot after translate). All handle drags un-rotate the mouse position into the image's local (pre-rotation) space before computing parameter deltas.

Pre-transform local geometry (constant for a given node): for each handle, its pre-transform position `h_local` and its **opposite anchor** `a_local`:

| Handle | `h_local` (pre-transform) | Opposite anchor `a_local` |
|---|---|---|
| corner TL | `(0,0)` | BR `(W,H)` |
| corner TR | `(W,0)` | BL `(0,H)` |
| corner BR | `(W,H)` | TL `(0,0)` |
| corner BL | `(0,H)` | TR `(W,0)` |
| edge top | `(W/2, 0)` | bottom edge mid `(W/2, H)` |
| edge bottom | `(W/2, H)` | top edge mid `(W/2, 0)` |
| edge left | `(0, H/2)` | right edge mid `(W, H/2)` |
| edge right | `(W, H/2)` | left edge mid `(0, H/2)` |

**Algorithm for a scale/stretch drag:**

1. `pivot_world = piv + t` (transformed pivot).
2. Express the current mouse in local space relative to pivot:
   `v = mouse − pivot_world`; `v_local = R(−θ) · v`.
   (`v_local` is now the mouse in the image's unrotated coordinate frame, relative to pivot.)
3. Determine the new scale per affected axis from the ratio of the mouse's local coordinate to the handle's local coordinate (relative to pivot). **Scale is recomputed absolutely from the current mouse position each move (not as an incremental delta), which avoids drift and naturally handles flip (mouse crossing the pivot flips the sign):**
   - For an X-affecting handle (corners, left/right edges): `sx_new = v_local.x / (h_local.x − piv.x)`.
   - For a Y-affecting handle (corners, top/bottom edges): `sy_new = v_local.y / (h_local.y − piv.y)`.
   - Clamp to `[0.01, 5.0]`.
   - Shift → uniform: set both axes to the dominant axis's ratio (preserve aspect).
4. Keep the **opposite anchor fixed in world space** by adjusting translate:
   ```
   t_new = t_old − R · (S_new − S_old) · (a_local − piv)
   ```
   (derived from `f_new(a_local) == f_old(a_local)`). This keeps the opposite corner/edge stationary while the dragged handle tracks the mouse — Fusion-default behavior, and correct under any rotation.
5. Return `{"scale_x": sx_new, "scale_y": sy_new, "translate_x": t_new.x, "translate_y": t_new.y}` (only the changed keys).

**Rotate handle:**

- `pivot_world = piv + t`.
- `angle = atan2(mouse.y − pivot_world.y, mouse.x − pivot_world.x)`.
- `rotation_new = degrees(angle) + 90°` (stalk starts pointing "up"), normalized to `[−180, 180]`.
- Shift → snap to nearest 15° (existing `angle_snap_enabled`).

**Center handle (move):** `translate_x/y += image_delta`. (Rotation-independent; unchanged.)

**Pivot handle:** set `pivot_mode = "custom"`; `pivot_x += image_delta.x`, `pivot_y += image_delta.y` (pivot lives in pre-transform image space; translate is separate). Note: moving the pivot does not move the image content — it only relocates the rotation/scale origin.

> **Fallback (if anchor-fixed math proves unstable in testing):** scale about the pivot instead — drop step 4, leave `t_new = t_old`. Simpler, still rotation-correct; only differs from Fusion when `pivot_mode == "custom"` with pivot ≠ center. Decision point flagged for confirmation if reached.

### 3.4 Fix the scale-classification bug (P4)

In both `_handle_transform_press` and `_handle_transform_move` (`composite_viewport.py`), replace:
```python
handle_type = handle.split("_")[0] if "_" in handle else handle
is_scale = handle_type in ("top","bottom","left","right","corner","edge")
is_rotate = handle_type == "rotate" or handle == "rotate"
is_translate = handle_type == "center" or handle_type == "pivot"
```
with classification by prefix:
```python
is_scale    = handle.startswith("scale_")            # scale_corner_*, scale_x_*, scale_y_*
is_rotate   = handle == "rotate"
is_translate = handle in ("center", "pivot")
```
This routes corner/edge drags into the scale branch of `_compute_new_params`.

### 3.5 Visual polish — Fusion-style OpenGL drawing (`composite_viewport.py`)

New/changed drawing primitives (all constant screen-pixel size via cosmetic-style NDC conversion, zoom-independent):

- **`_draw_overlay_quad(corners_ndc[4], color, pos_loc, color_loc)`** — line-loop through the 4 ordered transformed corners (the rotated frame). Replaces the AABB `_draw_overlay_rect` call in `_draw_transform_auxiliary`.
- **`_draw_overlay_square(ndc_pos, side_px, color, ...)`** — screen-aligned filled square with a dark outline, for corner & edge handles.
- `_draw_transform_auxiliary`: draw the rotated frame quad + the rotate stalk line (`stalk_base → rotate_handle`).
- `_draw_handles_from_dict`: corner/edge handles → squares; rotate → circle; center → white cross; pivot → smaller hollow/distinct cross. Keep existing hover(yellow)/active(orange)/selected colors.

Handle colors (from `viewer_handles.py`, already defined): scale-X green, scale-Y blue, rotate cyan, center/pivot white, corners white.

---

## 4. Files to Modify

| File | Changes |
|---|---|
| `src/features/composite/ui/viewer_handles.py` | Rewrite `compute_transform_handles` (ordered corners, edge midpoints, local axes, zoom-stable rotate stalk); rewrite `compute_transform_delta_params` (local-space scale, anchor-fixed translate, pivot_world rotate); extend `TransformHandlesData` (`frame_corners`, edge mids, `stalk_base`, `stalk_dir`). |
| `src/features/composite/ui/composite_viewport.py` | (a) Drawing decoupled from tool mode + selected-Transform gate in `_draw_tool_handles`; (b) handle-hit priority in `mousePressEvent`; (c) `_compute_transform_handles_dict` maps new positions; (d) `_draw_transform_auxiliary` draws quad + stalk; (e) `_draw_handles_from_dict` square handles; (f) add `_draw_overlay_quad` / `_draw_overlay_square`; (g) fix `is_scale`/`is_rotate`/`is_translate` classification in press & move. |
| `src/features/composite/ui/composite_page.py` | Verify only — `_compute_new_params` signature/keys unchanged. No edit expected. |

No changes to: `transform_node.py`, the GPU evaluator, the properties panel, node registration, or i18n.

---

## 5. Testing

- **Pure-function unit tests** (new) for `compute_transform_handles` and `compute_transform_delta_params`:
  - Identity params (all defaults) → 4 corners at image rectangle; edge mids at side midpoints; stalk vertical above top edge.
  - `rotation = 0`: corner-drag scales about opposite corner; edge-drag stretches single axis; opposite edge/corner stays fixed (assert world position unchanged).
  - `rotation = 30°` and `90°`: dragging a corner along the rotated local-X axis changes only `scale_x`; along local-Y only `scale_y`; opposite corner world-fixed.
  - Shift → `scale_x == scale_y`.
  - Rotate handle: mouse on stalk direction → `rotation ≈ 0`; rotate 90° → `rotation ≈ 90`.
  - Clamp: extreme drag → scale clamped to `[0.01, 5.0]`.
  - Flip: `scale_x < 0` → local axis inverted, handles still reachable.
- **Compile gate:** `python3 -m py_compile` on all three files (project uses `python3`, not `python`).
- **Manual smoke (documented, run by user):** select Transform node → handles appear; drag center/corner/edge/rotate/pivot in PAN and TRANSFORM modes; verify under rotation; verify undo = one step; verify deselect hides handles.

## 6. Edge Cases

- `rotation = 0` → behavior matches today's (working) translate/rotate path plus newly-working scale.
- `scale_x/y` → 0 clamped to 0.01; negative (flip) inverts local axis for placement and drag direction.
- `pivot_mode` `center` ↔ `custom`: dragging pivot switches to `custom`; pivot handle always rendered.
- Image not yet rendered (`_tex_width/height ≤ 0`): `_update_tool_handles` already clears handles — preserved.
- Interaction isolation: each handle type produces only its own param keys; no cross-contamination.
- Zoom/pan: handles are screen-space constant size (cosmetic); stalk length constant in screen px.

## 7. Out of Scope

- CornerPin / Mask / Roto rotation-geometry fixes (same root pattern, but user chose Transform-only).
- New Transform parameters (motion blur, skew, shear).
- Properties-panel UI changes.
- Refactoring the shared tool-mode model beyond the minimal decoupling in §3.1.

## 8. Risks

- **OpenGL primitive additions** (`_draw_overlay_quad`, `_draw_overlay_square`) must match the existing overlay-shader attribute layout (`in_vert`, `u_color`) and VBO pattern used by `_draw_overlay_rect`/`_draw_overlay_circle`. Low risk — follow existing helpers.
- **Anchor-fixed math (§3.3 step 4)** is the most derivation-heavy piece; mitigated by unit tests and the documented pivot-scale fallback.
- **Hit-priority change** must not break existing PAN (middle-mouse/space) or SELECT workflows — only adds a pre-check when a handle is under the cursor.
