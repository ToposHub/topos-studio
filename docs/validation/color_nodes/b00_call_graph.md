# B00.1 调色 Layers 数据 生产者/消费者调用图 — 现状冻结

生成日期：2026-11-02。基线 commit：`e43cec9`（Add DaVinci-style color node editor UI）。
本文件由 Explore agent 全库扫描产出，作为调色节点后端迁移（B00–B16）的现状基线。
行号为该工作区实测值，后续施工按函数名定位。

## 0. 架构总览

```
权威运行态:  ImageEngine.layers (List[Layer]) + ImageEngine.active_layer_index   (UI 线程可变)
权威持久态:  EditProject.color_stacks: dict[stack_id, ColorStackData]            (frozen dataclass, 不可变替换)
             ColorStackData.layers: tuple[dict]  ← Layer.to_dict(None) 全量快照 (schema v2 权威)
             平铺精简键 (grading_params/curves_data/hsl_curve_data/mixer_params/soft_clip_params/lut_path) ← 从活动层派生的兼容视图

生产(写 engine):  LayerPanel UI 信号 → layer_controller → engine.add_layer/remove_layer/layers[i].xxx
                  Grade 提交:      edit_controller._on_grading_changed_for_color_stack → UpdateColorStackCommand → project.color_stacks
                  水合(写 engine):  ColorStackHydrator.apply_stack_to_engine (clip 切换/版本切换/粘贴)
持久化(读 engine): SaveRouter._extract_full_color_data_from_engine / _extract_color_stacks_from_engine
                  edit_controller 提交时自行构造 layers_snapshot (不经 hydrator.collect)
消费(只读):        AsyncRenderer (经 _layer_render_data DTO)、timeline_preview_renderer、timeline_export、
                  composite color_evaluator (经 ColorStackRenderService)、LUT 导出、video_playback、scripting api
```

注意：全库**不存在 `set_layers` 方法**（rg 命中均为 `_prepare_preset_layers` 的子串误报）。
`AsyncRenderer.layers`（async_renderer.py:275/1304）是渲染线程经 `UPDATE_UNIFORMS` 命令缓存的
DTO 镜像（`_layer_render_data` 产物），不是 Layer 对象。

## 1. engine.layers / active_layer_index 写入点调用表

### 1a. 引擎本体（src/features/color/core/image_engine.py）

| 文件:行 | 函数/类 | 写入内容 | 触发 |
|---|---|---|---|
| image_engine.py:360-361 | `ImageEngine.__init__` | `layers=[Layer("Layer 1")]`, `active_layer_index=0` | 初始化 |
| image_engine.py:1220-1223 | `add_layer()` | layers.append 新层 + active=末位 + render | UI(LayerPanel)/脚本 |
| image_engine.py:1245-1248 | `remove_layer(index)` | layers.pop + active 钳位 + 删 baked EXR + `RenderCommand.REMOVE_LAYER` | UI/脚本 |
| image_engine.py:1253-1254 | `set_active_layer(index)` | 仅写 active_layer_index | UI/脚本 |
| image_engine.py:1324-1325 | `duplicate_layer(index)` | layers.append 拷贝 + active=末位 | UI/脚本 |
| image_engine.py:4672-4687 | `_restore_state(state)`（被 undo/redo 调） | **layers 整表替换**（`HistoryManager.deserialize_layers`）+ active 恢复 | undo/redo |
| image_engine.py:1201-1210 | `reset_all_layers()` | 每层原地 `reset_params()` + 清 GPU AUX | UI 重置 |
| image_engine.py:4915-4996 | `bake_layer/_unbake_layer_internal` | 层级 bake 状态（is_baked/baked_file_name/baked_texture_id） | UI bake |

### 1b. UI 控制器直接写（src/app/controllers/）

| 文件:行 | 函数 | 写入内容 | 触发 |
|---|---|---|---|
| layer_controller.py:230 | `on_layer_added` | 委托 engine.add_layer（先 `_push_layer_history` 226-233） | LayerPanel 信号 |
| layer_controller.py:239-244 | `on_layer_removed` | 委托 engine.remove_layer | LayerPanel 信号 |
| layer_controller.py:251-253 | `on_layer_moved` | `layers.pop/insert` + `active_layer_index=new_idx`（绕过 engine 方法） | LayerPanel 拖拽 |
| layer_controller.py:265 | `on_layer_visibility_changed` | `layers[index].visible` | LayerPanel |
| layer_controller.py:275 | `on_layer_renamed` | `layers[index].name` | LayerPanel / menu_toolbar:777 |
| layer_controller.py:288/295/309 | `on_layer_copy/paste/duplicate` | engine.copy_layer / paste_layer / duplicate_layer | LayerPanel |
| layer_controller.py:457-458 | `on_apply_grade_from_library` | **layers 整表替换**（深拷贝源层、剥离 AI matte/bake/AI 插件/BITMAP+BRUSH 蒙版 432-452）+ active 钳位 | Library 应用调色 |
| layer_controller.py:575-576 | `on_apply_grade_from_preset` | **layers 整表替换** + active 钳位 | Preset 应用 |
| layer_controller.py:883-921 | `on_apply_preset_categories_requested` | `clipboard.paste_onto_layers(engine.layers)` 按类别逐层打补丁 + push history | 分类应用 |
| layer_controller.py:1121-1146 | `_apply_auto_suggestion` | 逐层写 grading_params 键 + push history | Auto Color / Shot Match |
| layer_controller.py:2293-2307 | `on_layer_reset` | `layers[index].reset_params()` 原地重置 + push history | UI 重置 |
| file_controller.py:927-928 | `load_image`（offline 分支） | `engine.layers = layers; active=0` | 离线 baked 模式 |
| file_controller.py:1013/1055/1057 | `load_image` | **layers 整表替换**（.topos 数据或新建默认层） | 加载单素材工程 |
| file_controller.py:7160-7161 | `_load_video_project` | layers 整表替换 + active=0 | 加载视频工程 |
| deliver_controller.py:1950-1967 | `_batch_export_single_file` | **layers 整表替换** + active=0（每个批文件） | 批量导出 |
| deliver_controller.py:2127-2136 | `_batch_export_complete` | layers/active 恢复批处理前快照（1528-1535 拍照） | 批量导出结束 |
| edit_controller.py:2150-2152 | `_on_grading_changed_for_color_stack` | **所有层** grading_params 广播覆写 | GradingPanel 手势提交 |
| edit_controller.py:8878-8888, 9018-9028 | `_on_paste_grade_all` / `_paste_grade_to_clip` | 经 `hydrator.apply_stack_to_engine` 重建 layers | 粘贴调色 |
| menu_toolbar_controller.py:777-791 | rename/toggle visibility/disable_all | 间接走 layer_controller | 菜单/快捷键 |

### 1c. 水合器写入（src/features/color/adapters/color_stack_hydrator.py）

| 文件:行 | 函数 | 写入内容 | 触发 |
|---|---|---|---|
| color_stack_hydrator.py:249-253 | `apply_stack_to_engine`（v2 分支） | **engine.layers 原子整表替换**（`Layer.from_dict` 逐条容错 AUD-24200）+ active 对齐 | clip 切换/版本激活/粘贴 |
| color_stack_hydrator.py:265-301 | 同上（v1 回退） | 每层 reset + 平铺键覆写 | 旧栈无快照 |
| color_stack_hydrator.py:198-215 | 同上 | engine 级 stabilization/denoise 状态 | 同上 |
| color_stack_hydrator.py:309-321 | 同上 | tracking_store 整体 clear+重灌 | 同上 |
| color_stack_hydrator.py:533-544 | `hydrate` | apply engine + apply panels + render | page_controller.py:3178 |

### 1d. 脚本 API 写入（src/features/scripting/api.py）

api.py:496 `add_layer`、:504 `remove_layer`、:508 `select_layer`、:514/522/530
`set_layer_visible/opacity/blend_mode`、:537 `duplicate_layer`、:551 `reset_layer`。

## 2. EditProject.color_stacks 写入点（工程层）

| 文件:行 | 函数/类 | 写入内容 | 触发 |
|---|---|---|---|
| save_router.py:790-802 | `_save_color_to_edit_project`（栈已存在） | `edit_project.update_color_stack(...)` | 保存 |
| save_router.py:806-864 | 同上（栈不存在） | 新栈 + `color_stacks[stack_id]=new_stack` | 保存 |
| edit_controller.py:2128-2146 | `_on_grading_changed_for_color_stack` | `UpdateColorStackCommand.create(...)` → command stack | 手势提交 |
| edit_controller.py:8974-8994 | `_paste_grade_to_clip` | 同上 | 粘贴 |
| color_commands.py:219-268 | `UpdateColorStackCommand.apply/undo` | `project.update_color_stack(...)`（undo 用 `_old_layers` 拍照） | command |
| color_commands.py:49-86 | `CreateColorStackCommand` | create/delete_color_stack | command |
| color_commands.py:386-444 | `UpdateClipColorStackCommand` | clip.color_stack_id 引用切换 | command |
| edit_controller.py:176-203 | 模块级 helper | `_replace` 新 color_stacks dict | 编辑操作 |
| color_versions.py:286-743+ | 版本命令族 | `project.with_color_stacks(stacks)` 整表替换 | 版本 UI |
| shot_match_apply.py:61-170 | `apply_shot_match_batch` | 批量替换 color_stacks | Shot Match |
| project.py:2332-2719 | with/update/create/delete | frozen dataclass 不可变替换（**唯一合法写入 API**） | 上述所有 |

## 3. 读取点调用表（按消费场景）

- **渲染（DTO 快照）**：image_engine.py:102 `_layer_render_data`（模块级 DTO）→ :523/:3161 组装 render_params；async_renderer.py:1298-1306 `_do_update_uniforms` 缓存 DTO 镜像；:1257 `_do_remove_layer`；:2891 读 active_layer_index；video_playback.py:973/1935/2081。
- **撤销历史**：push_state 调用点 image_engine.py:1151,4722,4736,4753,4822；color_controller.py:83,206,253,2196,2338；layer_controller.py:219,426,566,883,1136；menu_toolbar_controller.py:1876；scripting/api.py:1316。history.py:75-210 push/undo/redo（`_serialize_layers`:233-249）。恢复唯一入口 image_engine.py:4620-4694 `_restore_state`。
- **保存/导出（读 engine）**：save_router.py:1304-1367 `_extract_color_stacks_from_engine`（standalone 单素材路径，`_extract_layer_data`:1369-1430 带 assets_dir 落盘位图）；save_router.py:1070-1139 `_extract_full_color_data_from_engine`（Color 页保存，layers 传 None 不落盘）；project_manager.py:332/504 旧路径；scripting/api.py:1504-1505 save_preset。
- **Timeline 预览（读 color_stacks，不读 engine）**：timeline_preview_renderer.py:4874-4984 `_get_preview_color_grade`（`has_advanced_operations`:4976、grade_keyframes 4929-4941、qualifier 4946-4953、chain_extra_params 4956-4964）；:10898-10934 `update_color_stacks`。
- **Timeline 导出**：timeline_export.py:9232-9457 `_get_clip_color_grade`；:9459+ `_get_clip_video_denoise`。
- **Composite**：color_evaluator.py:559-615 `_apply_topos_color` 经 `ColorStackRenderService.resolve_grading_params`:38-97（**平铺键 only，不消费 layers 快照**）+ color_chain 链段；node_engine.py 的 `_layers` 是合成图自有概念，与调色 Layer 无关；:2787+ `migrate_color_input_to_topos_color`。
- **其他**：deliver_controller.py:752-760 LUT 导出；delivery_matrix.py:225；plugin_panel.py:689-709；color_controller.py:282-311, 2630, 2665；edit_controller.py:2086-2097（**平铺键取 layers[0] 而非活动层**）；page_controller.py:3169-3178, 3615-3630；clip_version_host.py:696/779-798；color_versions/color_chain 多点；grade_clipboard.py:140/204。

## 4. 特别注意项

### 4.1 timeline_export.py — `layers_payload` 与 `has_advanced_operations`

位置：`_get_clip_color_grade`（timeline_export.py:9232-9457；预览侧镜像 timeline_preview_renderer.py:4860-4984，后者无 layers_payload 只有扁平键）。

- 每层独立进 `layers_payload`（:9372-9420，逐层 `is_advanced`:9397-9402：curves/hsl/soft_clip/curves_soft_clip/curves_edit/LUT/qualifier/masks/plugins），每条含 `index/active/opacity/grading_params/curves_data/hsl_curve_lut/soft_clip_params/curves_soft_clip/curves_edit/lut_path/lut_intensity/qualifier_params/mask_shapes/plugins`，`grade["layers"] = tuple(layers_payload)`（:9456）随 grade 透传（:6215/:10464 消费）。
- 扁平兼容填充：:9424-9439 仅在"恰好一层含该类内容"时等价回填顶层键。
- `has_advanced_operations`（:9443）= 栈级高级键 ∨ 层级 active_lut_path ∨ qualifier enabled ∨ **advanced_layer_count > 1**（多层高级组合超 CPU 扁平能力 → 硬门降级标记）。测试：tests/edit/test_m06_qualifier_export.py:54、test_n03_layers_mask_preflight.py:69。

### 4.2 save_router.py 数据流

- `_extract_full_color_data_from_engine`（:1070-1139）：活动层平铺键 + raw_params + trackings + video_stabilization/denoise + **layers 全量快照与 active_layer_index**（:1129-1137）→ `_save_color_to_edit_project`:774 → update_color_stack:790-802 / 新栈 replace:816-833 → 回调 :867 → :872。
- `_extract_color_stacks_from_engine`（:1304-1367）：standalone 单素材保存专用（:632 调用）；`_extract_layer_data`（:1369-1430）优先 `layer.to_dict(assets_dir)` 并附加 `index` 键（带 assets_dir → BITMAP/AI matte 落盘 PNG；full 路径传 None 不落盘）。

### 4.3 color_stack_hydrator.py 两个入口

- `apply_stack_to_engine`（:178-323）：v2（stack.layers 非空）→ 逐条 `Layer.from_dict`（坏层跳过）+ `restore_inline_bitmaps`（:137-164）→ 原子替换 engine.layers；v1 → reset+平铺键广播；另恢复 stabilization/denoise/tracking_store。调用方：page_controller.hydrate:3178、edit_controller:8879/9019、hydrator.hydrate:540。
- `collect_stack_from_engine_and_panels`（:376-527）：engine 有层时 `layer.to_dict(None)` + `attach_inline_bitmaps`（:102-134）→ layers_snapshot；平铺键从活动层派生（:432-446）。**现状：src/ 内无生产调用点**（生产走 edit_controller:2105-2126 与 save_router:1129-1137 两条平行实现）；仅测试引用。

### 4.4 冻结的已知口径问题（迁移时不得静默继承为新语义）

1. 引擎侧 `active_layer_index` 无 setter 保护；layer_controller.py:253 直接写；video_playback.py:2081 探测过私有名 `_active_layer_index`。
2. `edit_controller._on_grading_changed_for_color_stack`:2150-2152 把 grading_params **广播写到所有层**，而平铺快照派生 :2087 只看 `layers[0]`——两处口径不一致。
3. Grade 提交路径三处平行"快照+inline 位图"实现：edit_controller:2102-2109、edit_controller:8965-8972、hydrator.collect:420-422。

## 5. Layer 数据类字段清单（E01）

**身份/合成**：id, name, visible, opacity, blend_mode（BLEND_MODES 18 种显示名）。
**调色**：grading_params（lift/gamma/gain (h,s,b)、exposure、temp+temperature 双名、tint、contrast、shadow+shadows 双名、highlights、saturation、vibrance、mid_detail、sharpness、hue_shift、lift/gamma/gain_saturation）；curves_data + curves_lut(256,1,3) + curves_soft_clip + curves_edit；hsl_curve_data×6 + hsl_curve_lut(6,256)；mixer_params {8 通道 [h,s,l]}；warper_params(ndarray)；effects_params {enabled,mode,sharpen,texture,halo,radius,iterations}；active_lut_path/lut_intensity；soft_clip_params 9 键。
**Qualifier**：enabled, hue_enabled/center/width/soft, sat_enabled/low/high/soft_low/soft_high, lum_enabled/low/high/soft_low/soft_high, blur, clean_black, clean_white, denoise, invert。
**Window/蒙版**：mask_shapes（MaskShape: CIRCLE/RECT/CURVE/GRADIENT/BITMAP/BRUSH，operation ADD/SUBTRACT/INTERSECT，feather/softness/opacity/blur/clean/black/center/size/rotation/points/bitmap_data/brush_*；MaskGroup 嵌套）。
**插件**：plugins [{instance_id, plugin_id, params, enabled, type("dctl"|"ai")}]。
**AI/Bake**：ai_matte(float32 ndarray), ai_matte_operation(add/subtract/intersect), ai_matte_enabled；is_baked, baked_file_name(EXR), baked_texture_id。
**关键帧**：grade_keyframes {param → GradeKeyframeTrack}。
**缓存/脏标志**：cached_mask, cached_result, is_dirty, mask_dirty。

## 6. 相关现有测试

核心：test_layer.py、test_color_stack_hydrator.py、test_color_state_roundtrip.py、test_color_session.py、test_history.py、test_image_engine_{layers,history,state,params,render_data,coverage,transforms}.py、test_color_engine_triple_golden.py、test_color_e2e_consistency_matrix.py。
字段域：test_grade_clipboard.py、test_grade_keyframes.py、test_qualifier_*.py、test_plugin_*.py、test_ai_plugin_pixel_contract.py、test_bake_prefix_semantics.py、test_mask_*.py、test_power_windows.py、test_window_geometry_roundtrip.py、test_tracking_keyframe_bake.py。
tests/edit/：test_m06_qualifier_export.py、test_n03_layers_mask_preflight.py（断言导出 has_advanced_operations/layers payload）。
