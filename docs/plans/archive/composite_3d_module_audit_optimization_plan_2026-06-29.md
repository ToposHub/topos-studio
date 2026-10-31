# Composite 3D Module Audit and Optimization Plan

Date: 2026-06-29
Scope: Composite page 3D preview, Scene3D rendering, 3D source nodes, viewer interaction, cache/refresh behavior, and C4D-like viewport workflow.

Reference screenshots:
- Current Topos 3D preview: `/Users/testuser/Desktop/截屏2026-06-29 20.15.25.png`
- C4D target reference: `/Users/testuser/Desktop/截屏2026-06-29 20.14.29.png`

## Executive Summary

The current 3D module is a workable foundation, not yet a complete DCC-style viewport system. The core render path already supports Scene3D, Camera3D, Light3D, Geo3D, Model3D, MSAA, grid baking, payload-based scene inputs, and a basic move gizmo. However, several behaviors are still framework-level:

- World axis rendering exists but is disabled in the Scene3D call path.
- The move gizmo currently targets `scene3d` selection/params, while object transforms mostly live in Geo3D/Model3D after the node split.
- 3D view state, render state, and output/composite state are partially separated, but some cached results can still become stale if payload-only changes are not part of Scene3D's internal content signature.
- There is no C4D-style four-view layout, no 3D-specific toolbar, and no unified 3D tool/mode state for select/move/rotate/scale/point/edge/polygon/model/axis/snap/quantize.
- Performance is acceptable for a prototype, but repeated depth passes, full-scene redraw, IBL baking, resource lifetime, and model loading need clearer ownership and profiling.

Recommended approach: first stabilize correctness and refresh semantics, then add a dedicated 3D viewport controller and toolbar, then expand transform/selection modes, and finally optimize render performance.

## Current Architecture Map

| Area | Current files | Current role | Maturity |
|---|---|---|---|
| Scene render aggregator | `src/features/composite/nodes/scene3d_node.py` | Renders final color/depth texture from camera/light/geometry/model payloads | Prototype-plus, needs bug fixes |
| 3D source nodes | `camera3d_node.py`, `light3d_node.py`, `geo3d_node.py`, `model3d_node.py` | Emit side-channel scene payloads consumed by Scene3D | Functional but still transitional |
| Scene payload side channel | `src/features/composite/render3d/scene_payload.py` | Bypasses standard image texture cache for GL scene data | Correct direction, needs signatures |
| Grid/world axes renderer | `src/features/composite/render3d/gizmo_renderer.py` | Baked 3D helper grid and world axes | Implemented, axes not wired correctly |
| Transform gizmo overlay | `src/features/composite/render3d/gizmo_overlay.py` | UI overlay move gizmo with hit test and axis drag | Move-only; target ownership mismatch |
| Composite viewer | `src/features/composite/ui/composite_viewport.py` | 2D/3D mode, camera nav, overlay draw, mouse/key dispatch | Needs 3D viewport subsystem |
| Composite page orchestration | `src/features/composite/ui/composite_page.py` | Applies preview/composite overrides, preview target, refresh | Good direction, needs payload cache safety |
| Tests | `tests/composite/test_scene_payload.py`, `test_scene3d_msaa_grid.py`, `test_3d_preview_override.py` | Headless/static coverage for core behavior | Useful, lacks GL viewport integration |

## Critical Bugs and Hidden Risks

| Priority | Issue | Evidence | Impact | Fix strategy | Files |
|---|---|---|---|---|---|
| P0 | `show_axes` exists but is effectively disabled | `Scene3DNode.PARAM_DEFS` includes `show_axes`, but `_gizmo.draw(... show_axes=False ...)` is called only inside `if show_grid` | User cannot see world coordinate axes even when enabled | Call gizmo renderer when `show_grid or show_axes`; pass `show_axes=show_axes`; decide whether axes are preview-only or output-controllable | `scene3d_node.py`, `test_scene3d_msaa_grid.py` |
| P0 | Scene3D internal content signature does not include upstream payload content | `content_sig` mostly uses Scene3D's own params; Camera3D/Light3D/Geo3D/Model3D payload changes can enter `_do_evaluate` without changing this signature | Camera, light, geometry, animation, or model changes may reuse stale `_gpu_texture` | Add stable payload signatures: camera matrix/fov/near/far, light dirs/colors/IBL key, geo transform/resource revision, model revision/anim time; include in content_sig before early reuse | `scene3d_node.py`, `scene_payload.py`, tests |
| P0 | Gizmo writes object params to selected `scene3d`, but split nodes move object params to Geo3D/Model3D | `_update_gizmo_visibility` requires selected node type `scene3d`; drag emits `object_pos_*` to selected node | Drag may not move actual connected geometry after node split | Introduce active 3D object target resolution: selected Geo3D/Model3D if selected, otherwise Scene3D's primary connected geometry; route transform edits there | `composite_viewport.py`, `composite_page.py`, `geo3d_node.py`, `model3d_node.py` |
| P0 | Light3D IBL payload can carry `None` textures after bake failure | Scene3D `_set_shared_uniforms` assumes `irradiance_cube.use(...)` etc. | Render thread crash if Light3D bake fails and payload textures are None | Guarantee black fallback textures in Light3D, or in Scene3D replace invalid payload IBL with local black fallback before uniform upload | `light3d_node.py`, `scene3d_node.py` |
| P1 | Camera payload mode uses Scene3D's `cam_pitch` for grid horizon fade | Grid draw receives `cam_pitch_deg=cam_pitch`, but connected Camera3D may provide different view | Grid fade can be visually wrong with external camera | Compute pitch from `eye-target` or carry `pitch` in `CameraPayload` | `scene_payload.py`, `camera3d_node.py`, `scene3d_node.py` |
| P1 | `show_axes` has no composite-output policy | `output_show_grid/output_bg_opaque` exist, but no `output_show_axes` or explicit preview-only rule | If axes are enabled later they may leak into composite output or be impossible to output deliberately | Recommended: make world origin axes preview-only by default; add `output_show_axes` only if user truly wants axes baked into output | `scene3d_node.py`, `composite_page.py` |
| P1 | Middle mouse currently means 3D pan, not C4D four-view toggle | `mousePressEvent` maps middle drag to camera pan in 3D | Requested C4D-like MMB click to quad view conflicts with drag pan | Detect click vs drag: MMB press/release under movement threshold toggles single/quad; MMB drag pans; Alt+MMB always pans | `composite_viewport.py` |
| P1 | Tab 2D/3D switching depends on render-thread cache invalidation | Existing forced refresh is good, but view-mode overrides are snapshot-only | Any missing force refresh can keep stale grid/background | Keep `force_refresh`; add tests for render request reaching renderer and for preview target switch | `composite_page.py`, `render_scheduler.py`, `composite_renderer.py` |
| P2 | Transitional comments say nodes are placeholders although code is now partly implemented | Geo3D/Model3D docstrings still say "占位骨架" | Future AI may make wrong assumptions and duplicate or remove working code | Update comments to reflect actual implemented state and remaining gaps | `geo3d_node.py`, `model3d_node.py`, `camera3d_node.py`, `light3d_node.py` |

## Missing C4D-like Viewport Features

| Feature | Desired behavior | Current state | Implementation plan | Priority |
|---|---|---|---|---|
| World axis display | Red X, green Y, blue Z world axes visible like C4D | Renderer exists, disabled in call path | Wire `show_axes`; add axis length/label policy; optionally add top-right orientation widget | P0 |
| Selected object outline | Orange outline around selected object | Not implemented for 3D geometry | Add object-id or selected mesh outline pass; start with silhouette/edge overlay for selected Geo3D/Model3D | P1 |
| 4-view layout | MMB click toggles perspective/top/right/front | Not implemented | Add `ViewportLayoutMode.SINGLE/QUAD`; maintain four camera presets; render same Scene3D into 4 sub-rects or split viewer blit areas | P1 |
| Top viewport toolbar | Select, move, rotate, scale, point, edge, polygon, model, axis, quantize, snap | General composite toolbar exists, no 3D toolbar | Add `CompositeViewer3DToolbar` shown only in 3D mode; icon buttons with tooltips; route into 3D tool state | P1 |
| Tool modes | W/E/R move/rotate/scale; select mode | Only W is intercepted and move gizmo exists | Add `Viewer3DToolMode`: SELECT, MOVE, ROTATE, SCALE; keep 2D `ViewerToolMode` separate | P1 |
| Component selection | Point, edge, polygon, model modes | Not implemented | Add mesh topology access in payload; implement CPU raycast or GPU ID buffer; store selection sets per Geo3D/Model3D node | P2 |
| Axis/pivot mode | Enable axis/pivot editing independent from object transform | Not implemented | Add `axis_edit_enabled`; display separate pivot gizmo; update pivot params and transform math | P2 |
| Quantize | Step-based move/rotate/scale | Not implemented | Add quantize settings: move step, rotate step, scale step; apply during gizmo drag when enabled | P2 |
| Snap | Grid/vertex/edge/face/object snap | 2D snap exists, not 3D | Add 3D snap service; start with grid snap, then vertex/object snap | P2 |
| View labels | Perspective/Top/Right/Front labels | Not implemented | Paint small label in each viewport sub-rect | P1 |
| Orientation widget | Top-right mini XYZ orientation | Not implemented | Implement overlay axis triad drawn in screen space, independent from scene output | P2 |

## Recommended 3D View Architecture

Do not keep expanding ad-hoc booleans in `CompositeViewer`. Create a small 3D-specific controller layer:

| Proposed module | Responsibility |
|---|---|
| `src/features/composite/ui/viewport3d_state.py` | Dataclasses/enums for layout mode, active view, camera presets, active tool, component mode, snap/quantize settings |
| `src/features/composite/ui/viewport3d_toolbar.py` | C4D-like toolbar widget shown only when viewer mode is 3D |
| `src/features/composite/ui/viewport3d_controller.py` | Mouse/key dispatch for 3D navigation, MMB click/drag disambiguation, four-view switching, active target resolution |
| `src/features/composite/render3d/viewport_overlays.py` | Orientation widget, labels, selected outline overlay data |
| `src/features/composite/render3d/selection.py` | Raycast/ID-picking and component selection model |

This keeps 2D composite editing tools from becoming tangled with 3D DCC-style tools.

## Four-View Implementation Plan

| Step | Work | Notes |
|---|---|---|
| 1 | Add `ViewportLayoutMode.SINGLE`, `QUAD` and `ViewportViewKind.PERSPECTIVE/TOP/RIGHT/FRONT` | Store in viewer, not node params |
| 2 | MMB click toggles layout; MMB drag still pans | Use press position/time and movement threshold; Alt+MMB remains pan |
| 3 | Render/blit one texture into four sub-view rectangles initially | Fast first version: reuse current Scene3D render for perspective and draw orthographic labels/placeholder for orthographic until renderer supports per-view render |
| 4 | Add per-view camera matrices | Perspective uses orbit camera; top/right/front use orthographic cameras around scene target |
| 5 | Extend Scene3D preview request to accept preview camera override | Must not mutate Camera3D/Scene3D node params for temporary view layout |
| 6 | Add per-view labels and active viewport border | Match C4D reference visual language |

## Toolbar Implementation Plan

| Tool/control | UI type | Internal state | First behavior |
|---|---|---|---|
| Select | icon button | `tool=SELECT` | Object selection/raycast when available |
| Move | icon button, W | `tool=MOVE` | Existing move gizmo |
| Rotate | icon button, E/R depending local shortcut policy | `tool=ROTATE` | Add rotation rings |
| Scale | icon button | `tool=SCALE` | Add scale handles |
| Point | segmented/icon toggle | `component_mode=POINT` | Disabled until mesh picking exists |
| Edge | segmented/icon toggle | `component_mode=EDGE` | Disabled until mesh picking exists |
| Polygon | segmented/icon toggle | `component_mode=POLYGON` | Disabled until mesh picking exists |
| Model | segmented/icon toggle | `component_mode=OBJECT` | Default |
| Enable Axis | toggle | `axis_edit_enabled` | Later pivot editing |
| Quantize | toggle/menu | `quantize_enabled`, step settings | Move snap to step |
| Snap | toggle/menu | `snap_enabled`, snap modes | Start with grid snap |

## Rendering and Cache Optimization Plan

| Priority | Optimization | Why | Implementation |
|---|---|---|---|
| P0 | Add upstream payload signatures to Scene3D content cache | Prevent stale output when Camera3D/Light3D/Geo3D/Model3D changes | Add `signature()` methods or helper hash for payload dataclasses |
| P1 | Avoid full cache clear on every view-mode switch where possible | Current forced refresh is safe but coarse | Keep force refresh for correctness; later narrow it to affected Scene3D/Output nodes |
| P1 | Reduce duplicate depth pass cost | Scene3D redraws geometry for color and then depth texture | Explore MRT or depth resolve/read depth attachment into texture where supported |
| P1 | Async model loading and GPU upload staging | Large glTF/FBX blocks render thread | Load/parse on worker, upload GL resources on render thread |
| P1 | Better resource release for MeshVBOPool when ctx is unavailable | `cleanup()` often calls `_release_flat_meshes_gl()` without ctx, so pooled VBO/IBO refs may not decrement | Store pool/context identity in node or make pool release by stored owner |
| P2 | LOD during navigation | Heavy models should stay interactive | During nav drag, render lower MSAA/no shadows/lower sample count, restore on release |
| P2 | GPU picking/id buffer | Needed for object/component selection | Add optional integer/object-id render pass for active viewport |
| P2 | Frustum and instance culling metrics | Need to know if culling pays off | Add perf counters to Scene3D result metadata/log panel |

## Test Plan

| Area | Tests to add |
|---|---|
| Axis rendering | Static test that `_gizmo.draw` passes `show_axes=show_axes`; behavior test that `show_axes=True/show_grid=False` still calls draw |
| Payload cache signature | Unit tests that camera/light/geo/model payload changes force Scene3D revision even if Scene3D params unchanged |
| Gizmo target routing | Tests for selected Geo3D, selected Model3D, selected Scene3D-with-connected-Geo3D |
| MMB quad toggle | QMouseEvent tests: click toggles quad; drag pans; Alt+MMB pans and does not toggle |
| Toolbar state | Tests that toolbar is visible only in 3D mode and updates `Viewer3DToolMode` |
| 2D/composite cleanliness | Existing grid/background tests plus future axes rule |
| GL smoke | Optional local GPU smoke test rendering cube + grid + axes, compare nonblank pixels |

## 3D Node-Specific Follow-up Audit

This section is a second pass focused only on the 3D node family and the UI/graph entry points that create or edit those nodes.

### Scene3D Node

| Priority | Issue / missing feature | Evidence | Impact | Fix strategy |
|---|---|---|---|---|
| P0 | Axes parameter is present but not honored | `show_axes` is defined, but `_gizmo.draw(... show_axes=False ...)` is hard-coded | User sees no world axes in 3D preview | Gate helper draw on `show_grid or show_axes`; pass both values through |
| P0 | Upstream scene payloads are collected by type, not by actual connected port | `_collect_scene_payload_by_type()` scans all side-channel payloads globally | If multiple Camera3D/Light3D/Geo3D/Model3D nodes exist, Scene3D can consume the wrong one | Add port-aware payload collection: read connections for `camera_in`, `lights_in`, `geometry_in`, then fetch payload by connected node id |
| P0 | Model3D vs Geo3D priority is too coarse | Scene3D uses first `ModelPayload(has_model=True)` globally and otherwise first `GeometryPayload` | Multiple geometry inputs, mixed procedural/model scenes, or disconnected payloads can render incorrectly | Build a connected geometry payload list from `geometry_in`; render all connected Geo3D/Model3D payloads in order, not just first by type |
| P0 | Scene3D cache signature ignores connected payload details | Existing `content_sig` mostly covers Scene3D params | Connected Camera/Light/Geo/Model changes can reuse stale `_gpu_texture` | Add payload signatures and include connection topology + upstream payload revisions |
| P1 | Scene3D still keeps legacy geometry/model/light params partially alive | Migration keeps only selected params, but drag/drop can still create Scene3D with `model_path/use_imported_model`; Scene3D code still supports old monolithic path | New graph creation and migrated graph semantics can diverge | Pick one primary path: for new graphs create child nodes; keep legacy path only as compatibility fallback |
| P1 | Scene3D panel may expose internal/default camera and helper params without explaining split-node workflow | `_build_scene3d_ui` builds direct rows for all remaining Scene3D params | Users may edit Scene3D camera while connected Camera3D overrides it, causing "parameter changes do nothing" confusion | In properties panel, group Scene3D params into Output/Preview/Fallback Camera; show warning or disable fallback camera controls when Camera3D is connected |
| P1 | Depth pass does not clearly cover helper grid/axes policy | Color pass can bake grid; depth pass redraws geometry only | Deep compositing may not match helper visibility if grid is intentionally output | Decide helpers never write depth for composite, or explicitly include output grid depth when `output_show_grid=True` |
| P2 | No render diagnostics are returned to UI | Model load errors and fallback states exist internally but are not surfaced on Scene3D output | Users see blank/gray output without actionable reason | Add status metadata: active camera, light count, geometry count, model error, IBL fallback, MSAA fallback |

### Camera3D Node

| Priority | Issue / missing feature | Evidence | Impact | Fix strategy |
|---|---|---|---|---|
| P1 | Camera3D only supports orbit camera | Params are `cam_dist`, `cam_orbit_yaw`, `cam_orbit_pitch`, `target` | No free camera, orthographic camera, film back, clipping control, or per-view camera needed for top/right/front | Add camera mode enum: perspective orbit, perspective free, orthographic; add near/far, ortho scale, roll |
| P1 | Width/height duplicated on Camera3D and Scene3D | CameraPayload aspect is derived from Camera3D width/height, while Scene3D also has width/height | Aspect can mismatch render target if user edits only one node | Scene3D should override camera aspect at render time, or page should sync format into connected Camera3D |
| P1 | 3D navigation target resolution only handles Camera3D when Scene3D is selected | `_resolve_nav_target()` redirects Scene3D camera input to Camera3D, but selecting Camera3D has no preview image | Camera editing is awkward; camera node cannot show the scene by itself | When Camera3D selected, locate downstream Scene3D and preview that Scene3D while routing nav edits to Camera3D |
| P2 | No camera framing tools | Only F reset exists in viewer | Users need frame selected/all, save view, restore view, default camera switch | Add frame selected/all based on scene bounds; add camera preset storage |
| P2 | No visual camera object in 3D scene | Camera3D has no viewport representation | Hard to understand multiple cameras | Add camera frustum/icon overlay when not viewing through that camera |

### Light3D Node

| Priority | Issue / missing feature | Evidence | Impact | Fix strategy |
|---|---|---|---|---|
| P0 | IBL fallback can emit `None` textures | `Light3DNode` catches bake errors but may still leave `irradiance_cube/prefilter_cube/brdf_lut` as None | Scene3D can crash in `_set_shared_uniforms()` when calling `.use()` | Guarantee black placeholder textures before emitting payload; Scene3D should defensively replace invalid IBL |
| P1 | `hdri_path` is plain string, unlike Scene3D `hdri_preset` type | Light3D `ParamDef("hdri_path", "str", "")` | Properties panel gives poor file selection UX for HDRI | Use `ptype="file"` or a dedicated `hdri` param type with preset picker |
| P1 | Only directional lights are modeled | Light payload is fixed dirs/colors/intensities | No point, spot, area, or visible light gizmos | Add light type enum per slot; extend shader/light payload gradually |
| P1 | Multiple Light3D nodes are ambiguous | Scene3D currently collects first LightPayload by type | Connecting several lights nodes or leaving old payloads in graph can produce wrong lighting | Port-aware collection, then either merge all connected Light3D payloads or allow exactly one with validation |
| P2 | No light viewport manipulation | Light direction only via numeric fields | Poor interactive workflow compared with DCC apps | Add light direction gizmo, light icons, and "look through selected light" if useful |

### Geo3D Node

| Priority | Issue / missing feature | Evidence | Impact | Fix strategy |
|---|---|---|---|---|
| P0 | Geometry VAO cache ignores instance-enabled changes | `_ensure_mesh_geometry()` early-reuses when primitive and VBO exist, without checking `instance_enabled` | Toggling instancing can leave a VAO bound to the wrong shader/attributes | Include `instance_enabled` and instance buffer signature in the reuse condition; force VAO rebuild on toggle |
| P1 | No component topology selection data in payload | GeometryPayload contains VAO/program/material/model matrix, not editable topology/ids | Point/edge/polygon modes cannot be implemented cleanly | Carry mesh metadata or a CPU-side topology reference for picking/selection |
| P1 | Only basic primitives are available | primitive options: cube/plane/grid/sphere | Missing cylinder, cone, torus, text/spline/extrude, null/axis object | Add common primitives and a null object source |
| P1 | Object transform ownership conflicts with Scene3D gizmo | Geo3D owns `object_pos/rot`, but viewer gizmo currently appears only for selected Scene3D | Dragging the gizmo may not move Geo3D | Viewer/page must resolve active transform node and write Geo3D params |
| P2 | Instance controls are grid-only | Params are `grid_x`, `grid_z`, `spacing` | No randomization, per-instance transforms, scatter, or object list | Add instancer node later rather than growing Geo3D too much |
| P2 | No material node/input | Material is embedded as scalar params | Hard to reuse material across Geo3D and Model3D | Add Material3D payload node or material input port later |

### Model3D Node

| Priority | Issue / missing feature | Evidence | Impact | Fix strategy |
|---|---|---|---|---|
| P0 | Model3D has no object transform params | It only has `model_path/use_imported_model/anim_*`; transform params live in Geo3D or Scene3D legacy path | Imported models cannot be moved/rotated/scaled as their own node in split architecture | Add object transform params to Model3D or introduce a Transform3D node that wraps any scene payload |
| P0 | Drag-and-drop model creation still creates Scene3D with legacy `model_path/use_imported_model` params | `node_editor_widget.dropEvent()` routes 3D model files to `scene3d` and writes model params | New projects bypass the split-node architecture and can confuse UI/gizmo routing | Drop should create Scene3D + Model3D + Camera3D/Light3D/Geo3D as needed, then connect Model3D to `geometry_in` |
| P1 | Model load error is stored but not surfaced | `_model_error` is emitted in ModelPayload, but UI does not show it prominently | User sees fallback/empty output without knowing why | Show error banner in properties panel and Scene3D preview overlay/status |
| P1 | Model cache release may miss MeshVBOPool ref decrement | `cleanup()` calls `_release_flat_meshes_gl()` without ctx, and that path cannot release pool refs | Possible GPU resource leak for repeated model loads | Store pool key/context when acquiring or provide pool release independent of ctx |
| P1 | No material texture support visible in node params | Scene graph material has scalar PBR only; loader/render path likely lacks base color/normal/roughness textures | Imported glTF models look flat/inaccurate | Add texture slots and loader support for baseColor/normal/ORM/emissive maps |
| P1 | Animation selection is missing | Model3D samples first clip only | Multi-animation files cannot choose clips | Add `anim_clip` option, duration metadata, loop mode, playback mapping to composite frame |
| P2 | No units/up-axis/import options | Loader normalizes scene but user cannot control scale/up axis | Imported models may appear wrong size/orientation | Add import scale, normalize toggle, up-axis conversion, recenter toggle |

### Model3D UV, Texture, FBX, and Animation Audit

Current status after code inspection:

| Capability | Current support | Evidence | Practical result | Needed work |
|---|---|---|---|---|
| FBX geometry import | Partial | `model_loader.py` routes `.fbx` through `pyassimp`; `_convert_assimp_mesh()` reads vertices, normals, faces, and `texcoords[0]` | FBX mesh geometry can load if `pyassimp` and native `libassimp` are installed | Add dependency checks in UI; improve error display; add test assets/smoke tests |
| UV coordinates | Present as vertex attribute | `MeshData` stores `uvs`; trimesh path reads `visual.uv`; assimp path reads first UV channel; mesh shaders pass `in_uv -> v_uv` | UV data is uploaded to GPU, but currently mostly unused by material shader | Keep UV channel; add second UV set support later for AO/lightmap if needed |
| Base color texture | Not supported | `MaterialData` only stores `base_color` scalar/vector; `_MESH_PBR_FRAGMENT` has `u_albedo` but no `u_base_color_map` | Imported textured models render as flat material color, not with image texture | Add texture metadata to `MaterialData`, upload image textures, add sampler uniforms and fallback scalar |
| Normal map | Not supported | Shader normal is only interpolated vertex normal; no tangent/bitangent data, no normal sampler | Normal-mapped FBX/glTF assets lose surface detail | Add tangent generation/import, TBN basis, `normal_map` sampler, strength control |
| Metallic/Roughness maps | Not supported | Shader only has scalar `u_metallic/u_roughness` | ORM/PBR maps from glTF/FBX are ignored | Add packed ORM support and separate override maps |
| AO map | Not supported | No AO field in `MaterialData`, no AO sampler in shader | Ambient occlusion texture is ignored | Add `ao_map` or ORM channel extraction; multiply indirect diffuse/specular appropriately |
| Emissive map/color | Not supported | No emissive fields or shader contribution | Emissive model parts render dark/non-emissive | Add emissive color/map/intensity and add to final color |
| Texture color management | Not implemented | No image texture loader path for material maps | BaseColor should be sRGB->linear; normal/ORM/AO must stay data-linear | Add map role metadata and upload conversion policy |
| glTF animation | Very limited / best effort | `_extract_trimesh_animations()` reads `scene.metadata["animations"]` if present; `Model3DNode` samples first clip only | Some glTF files may animate, many will not depending on trimesh metadata shape | Prefer pygltflib/assimp animation extraction for glTF; add clip selection and robust channel mapping |
| FBX animation | Not effectively supported | Assimp loader converts node hierarchy/meshes/material scalars but does not convert assimp animations or bones into `AnimationClip`/`SkinData` | FBX animations will generally not play | Convert `scene.animations` and bone weights from assimp, or document FBX as static mesh until implemented |
| Skinning | Limited / fragile | glTF skin extraction expects custom metadata keys; assimp path does not populate skin data | Skinned animation is unreliable | Implement loader-specific skin extraction with tests; validate bone matrix space |

Recommended Model3D material input ports:

| Proposed input port | Type | Purpose | Default behavior |
|---|---|---|---|
| `base_color_in` | image | Override imported base color/albedo texture | If disconnected, use imported baseColor texture; if missing, use scalar base color |
| `normal_in` | image | Override normal map | If disconnected, use imported normal texture; if missing, use vertex normals |
| `metallic_in` | image | Override metallic map or metallic channel | If disconnected, use imported ORM/metallic map or scalar |
| `roughness_in` | image | Override roughness map or roughness channel | If disconnected, use imported ORM/roughness map or scalar |
| `ao_in` | image | Override ambient occlusion map | If disconnected, use imported occlusion/ORM map or 1.0 |
| `emissive_in` | image | Optional emissive texture override | If disconnected, use imported emissive map or black |
| `mask_in` | image | Optional material/object mask for selective override | Disconnected means full override |

Recommended parameter additions:

| Param | Purpose |
|---|---|
| `material_mode`: `imported / override / mix` | Decide whether external input textures replace or mix with imported material textures |
| `base_color_space`: `sRGB / Linear / Auto` | Correct color conversion for baseColor |
| `normal_format`: `OpenGL / DirectX` | Flip green channel when needed |
| `orm_layout`: `glTF ORM / separate / custom channels` | Decode occlusion/roughness/metallic packed textures |
| `texture_filter`: `nearest / linear / mipmap` | Texture sampling quality |
| `texture_wrap`: `repeat / clamp / mirror` | UV outside-range behavior |
| `emissive_intensity` | Scale emissive contribution |
| `anim_clip`, `anim_loop`, `anim_frame_mode` | Choose clip and map animation time to composite frames |

Implementation route:

1. Extend `MaterialData` into `Material3DData` with scalar factors plus optional texture descriptors for baseColor, normal, metallic, roughness, ao, emissive, and packed ORM.
2. Add a material texture loader/cache in render thread, resolving relative paths next to the model file.
3. Add optional Model3D image input ports for texture overrides. These should accept ordinary composite image outputs so users can procedurally generate or color-correct material maps.
4. Extend `ModelPayload.flat_meshes` entries with material texture handles or descriptors, plus override texture bindings.
5. Update `_MESH_PBR_FRAGMENT` to support `u_has_base_color_map`, `u_has_normal_map`, `u_has_metallic_map`, `u_has_roughness_map`, `u_has_ao_map`, `u_has_emissive_map`.
6. Generate/import tangent space for normal mapping. Without tangent data, normal maps should be disabled with a clear warning.
7. Add tests: UV preservation, scalar material fallback, texture descriptor extraction, shader source contains material samplers, FBX static import fallback, animation clip metadata parsing.

### Node Creation, Migration, and Properties

| Priority | Issue / missing feature | Evidence | Impact | Fix strategy |
|---|---|---|---|---|
| P0 | New-node creation path and migration path disagree | Migration creates split nodes; file drop creates monolithic Scene3D with model params | Users get different node graphs depending on how they create 3D content | Centralize a `create_default_3d_scene()` graph command and reuse it for menu, drop, migration tests |
| P1 | Scene payload collection ignores actual graph edges | Base helper scans all scene payloads by type | Disconnected scene nodes can affect a Scene3D render | Add `NodeGraph`-aware collection in Scene3D or pass connected upstream ids into eval context |
| P1 | 3D source nodes are categorized as source/image-like in generic systems | Capabilities use `roi_policy="full_frame"` and source scheduling, while they return no TextureResult | Some generic image-only UI/cache/preview assumptions may treat scene nodes as images | Add explicit `data_kind="scene"` capability or `produces_image=False` to distinguish scene source nodes |
| P1 | Properties panel lacks 3D-specific grouped UX except Scene3D preview/MSAA | Camera/Light/Geo/Model use generic parameter rows | Hard to use compared with DCC tools | Add dedicated sections for Camera3D, Light3D, Geo3D, Model3D with grouped controls, file pickers, warnings |
| P2 | i18n/labels for new 3D controls are incomplete | Search shows generic node names but no complete C4D-style toolbar labels yet | UI will feel inconsistent in Chinese/English | Add locale keys for 3D toolbar, view labels, component modes, snap/quantize |

## Suggested Execution Phases

| Phase | Goal | Deliverables |
|---|---|---|
| Phase 1: correctness hotfixes | Make existing 3D preview reliable | Axis visible; port-aware payload collection; payload signatures; Light3D IBL fallback; Geo3D VAO rebuild fix; Model3D transform ownership |
| Phase 2: node creation and split-graph cleanup | Make new and migrated 3D graphs consistent | Shared 3D scene creation command; drag/drop creates split nodes; Scene3D panel clarifies fallback-only params |
| Phase 3: 3D view state split | Prevent future interaction debt | `viewport3d_state/controller`; clean separation from 2D tools |
| Phase 4: C4D-like viewport shell | Match requested workflow visually | MMB quad view; view labels; 3D toolbar; active viewport border |
| Phase 5: transform tools | Make editing useful | Move/rotate/scale gizmos; object outline; undo/redo per transform |
| Phase 6: component and snapping tools | DCC-style modeling controls | Point/edge/polygon/model modes; grid snap; quantize |
| Phase 7: performance pass | Make large scenes interactive | Async loading, LOD during nav, depth-pass optimization, perf counters |

## Immediate Patch Checklist

1. In `scene3d_node.py`, change the helper draw gate from `if show_grid:` to `if show_grid or show_axes:`.
2. Pass `show_grid=show_grid` and `show_axes=show_axes` to `GizmoRenderer.draw`.
3. Decide policy for axes in composite output:
   - Preferred: axes are 3D preview helpers only, never baked into composite output.
   - Optional: add `output_show_axes=False` if users need axes in final output.
4. Add a payload signature to Scene3D's content signature.
5. Make TransformGizmo resolve and write the actual geometry node, not always Scene3D.
6. Add a black IBL fallback guarantee when Light3D payload cubemaps are unavailable.
7. Add MMB click/drag disambiguation before implementing quad view.
8. Make Scene3D collect scene payloads by connected port/node id, not by first payload type globally.
9. Fix Geo3D VAO reuse so `instance_enabled` changes rebuild the VAO.
10. Change 3D model file drop to create a split Scene3D + Model3D graph, not a monolithic Scene3D with legacy model params.
11. Add transform params to Model3D or introduce a Transform3D node before expanding gizmo editing.

## Notes for Future AI Implementation

- Preserve the current separation: 3D preview helpers are allowed to be visible in 3D mode, but final composite output should stay transparent/clean by default.
- Do not remove the payload side channel unless the entire evaluator/cache contract is redesigned.
- Avoid implementing four-view by mutating Camera3D node params on every temporary viewport render; view layout cameras are viewer state, not node graph state.
- Keep undo entries only for real edits: object transforms, camera commits, selection changes if persisted. Pure view layout/tool mode changes should not dirty the project.
- Prefer adding a focused 3D controller instead of continuing to grow `CompositeViewer` into a single large interaction class.
- Treat disconnected 3D source nodes as inert. A Scene3D render should be driven only by nodes connected to that Scene3D's scene input ports.
