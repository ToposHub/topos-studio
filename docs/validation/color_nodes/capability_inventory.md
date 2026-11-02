# 能力汇总与旧 layers 写入口清单（B16.1）

- 时间：2026-11-02T22:32:32；git `1405f67`
- action 表：B01 契约（UI envelope → service command）；provider 表：EffectCatalog（效果注册）；backend 表：图算子三后端矩阵（B12.1，实测见 backend_matrix.md）

## 1. Action 表（39 项）

| action | category | command |
|---|---|---|
| add_alpha_output | structure | add_alpha_output |
| add_input | structure | add_mixer_slot |
| add_key_mixer | structure | add_key_mixer |
| add_layer | structure | add_layer_branch |
| add_node | structure | add_node |
| add_outside | structure | add_outside_corrector |
| add_parallel | structure | add_parallel_branch |
| add_serial | structure | insert_corrector |
| add_serial_before | structure | insert_corrector |
| add_splitter_combiner | structure | add_splitter_pair |
| auto_layout | layout | update_layout |
| close_compound | special | close_compound |
| connect | structure | connect_ports |
| delete_nodes | structure | delete_nodes |
| disconnect | structure | disconnect_edge |
| duplicate_nodes | structure | duplicate_subgraph |
| expand_compound | structure | ungroup_subgraph |
| extract_nodes | structure | extract_nodes |
| label_node | metadata | set_node_metadata |
| lock_node | policy | set_edit_lock |
| make_compound | structure | group_subgraph |
| morph_mixer | parameter | change_mixer_type |
| move_and_insert | structure | move_and_insert |
| move_nodes | layout | update_layout |
| node_cache | policy | set_cache_policy |
| node_color | layout | set_node_color |
| paste_grade | parameter | paste_grade_domains |
| paste_nodes | structure | paste_subgraph |
| redo | history | graph_redo |
| remove_alpha_output | structure | remove_alpha_output |
| reset_all | reset | reset_graph |
| reset_grade | parameter | reset_corrector_parameters |
| reset_grades | reset | reset_graph_parameters |
| save_shared | definition | create_shared_definition |
| set_all_enabled | parameter | set_all_enabled |
| set_enabled | parameter | set_bypass |
| set_node_option | parameter | set_node_option |
| undo | history | graph_undo |
| unlink_shared | definition | make_local_corrector |

## 2. Provider 表（EffectCatalog，49 效果）

| effect | category | shader kind |
|---|---|---|
| blur | filter | separable_passes |
| defocus | filter | single_pass |
| denoise | filter | single_pass |
| edge_detect | filter | single_pass |
| erode_dilate | filter | single_pass |
| lib1_posterize | filter | single_pass |
| sharpen | filter | single_pass |
| plugin:Anamorphic_Streak | plugin | dctl_process |
| plugin:Bleach_Bypass | plugin | dctl_process |
| plugin:Bokeh_Defocus | plugin | dctl_process |
| plugin:CRT_Scanlines | plugin | dctl_process |
| plugin:Channel_Mixer | plugin | dctl_process |
| plugin:Color_Space_Transform | plugin | dctl_process |
| plugin:Cross_Process | plugin | dctl_process |
| plugin:Day_Night_Converter | plugin | dctl_process |
| plugin:Duotone_Toner | plugin | dctl_process |
| plugin:Edge_Ink_Outline | plugin | dctl_process |
| plugin:Fake_Infrared | plugin | dctl_process |
| plugin:Film_Grain | plugin | dctl_process |
| plugin:Film_Halation | plugin | dctl_process |
| plugin:Frequency_Split | plugin | dctl_process |
| plugin:Glitch_Blocks | plugin | dctl_process |
| plugin:God_Rays | plugin | dctl_process |
| plugin:Grad_Filter_Exposure | plugin | dctl_process |
| plugin:Halation_Extended | plugin | dctl_process |
| plugin:Halftone_Print | plugin | dctl_process |
| plugin:HighPass_Retouch | plugin | dctl_process |
| plugin:Kaleido_Mirror | plugin | dctl_process |
| plugin:LIB1_Gradient_Map | plugin | dctl_process |
| plugin:LIB1_Median_Rank | plugin | dctl_process |
| plugin:Lens_Distortion | plugin | dctl_process |
| plugin:Light_Leak_Corner | plugin | dctl_process |
| plugin:Motion_Blur_Linear | plugin | dctl_process |
| plugin:Pixel_Blocks | plugin | dctl_process |
| plugin:Prism_Aberration | plugin | dctl_process |
| plugin:Radial_Blur_Pro | plugin | dctl_process |
| plugin:Skin_Even_Matte | plugin | dctl_process |
| plugin:Sky_Replace_Matte | plugin | dctl_process |
| plugin:Soft_Glow_Bloom | plugin | dctl_process |
| plugin:Three_Way_Tint | plugin | dctl_process |
| plugin:Tilt_Shift_Focus | plugin | dctl_process |
| plugin:Unsharp_Sharpen | plugin | dctl_process |
| plugin:Vibrance_Saturation | plugin | dctl_process |
| plugin:Vignette_Shapes | plugin | dctl_process |
| plugin:White_Balance_Advanced | plugin | dctl_process |
| plugin:bilateral_skin_smooth | plugin | dctl_process |
| plugin:warm_tint | plugin | dctl_process |
| glow | stylize | separable_passes |
| video_stabilization | transform | single_pass |

## 3. Backend 表（图算子三后端）

| op | gl | cpu | metal |
|---|---|---|---|
| alpha_output | exact | exact | exact |
| combiner | exact | exact | exact |
| compound | pending | unsupported | unsupported |
| corrector | exact | fallback | fallback |
| key_mixer | exact | exact | exact |
| layer_mixer | exact | exact | exact |
| output | exact | exact | exact |
| outside | exact | fallback | fallback |
| parallel_mixer | exact | exact | exact |
| shared_corrector | exact | fallback | fallback |
| source | exact | exact | exact |
| splitter | exact | exact | exact |
| corrector:param_domains | exact | fallback | fallback |

UI 可选项：12 项；ui_enabled=False：无（provider 未实现如实禁用）

## 4. 旧 layers 写入口扫描（src/）

| 文件:行 | 分类 | 代码 |
|---|---|---|
| src/app/controllers/deliver_controller.py:1950 | 工程加载/交付（legacy 载荷→引擎） | `self.engine.layers = project_data.get('layers') or [Layer("Layer 1")]` |
| src/app/controllers/deliver_controller.py:1955 | 工程加载/交付（legacy 载荷→引擎） | `self.engine.layers = project_data` |
| src/app/controllers/deliver_controller.py:1958 | 工程加载/交付（legacy 载荷→引擎） | `self.engine.layers = [Layer("Layer 1")]` |
| src/app/controllers/deliver_controller.py:1965 | 工程加载/交付（legacy 载荷→引擎） | `self.engine.layers = [Layer("Layer 1")]` |
| src/app/controllers/deliver_controller.py:1967 | 工程加载/交付（legacy 载荷→引擎） | `self.engine.active_layer_index = 0` |
| src/app/controllers/deliver_controller.py:2127 | 工程加载/交付（legacy 载荷→引擎） | `self.engine.layers = (` |
| src/app/controllers/deliver_controller.py:2132 | 工程加载/交付（legacy 载荷→引擎） | `self.engine.active_layer_index = (` |
| src/app/controllers/file_controller.py:927 | 工程加载/另存（legacy 载荷→引擎） | `self.engine.layers = layers` |
| src/app/controllers/file_controller.py:928 | 工程加载/另存（legacy 载荷→引擎） | `self.engine.active_layer_index = 0` |
| src/app/controllers/file_controller.py:1013 | 工程加载/另存（legacy 载荷→引擎） | `self.engine.layers = project_data.get('layers') or [Layer("Layer 1")]` |
| src/app/controllers/file_controller.py:1055 | 工程加载/另存（legacy 载荷→引擎） | `self.engine.layers = project_data` |
| src/app/controllers/file_controller.py:1057 | 工程加载/另存（legacy 载荷→引擎） | `self.engine.layers = [Layer("Layer 1")]` |
| src/app/controllers/file_controller.py:7160 | 工程加载/另存（legacy 载荷→引擎） | `self.engine.layers = layers` |
| src/app/controllers/file_controller.py:7161 | 工程加载/另存（legacy 载荷→引擎） | `self.engine.active_layer_index = 0` |
| src/app/controllers/layer_controller.py:259 | 过渡 LayerPanel（B16.1：仅过渡使用，退场记账） | `layer = self.engine.layers.pop(old_idx)` |
| src/app/controllers/layer_controller.py:260 | 过渡 LayerPanel（B16.1：仅过渡使用，退场记账） | `self.engine.layers.insert(new_idx, layer)` |
| src/app/controllers/layer_controller.py:261 | 过渡 LayerPanel（B16.1：仅过渡使用，退场记账） | `self.engine.active_layer_index = new_idx` |
| src/app/controllers/layer_controller.py:465 | 过渡 LayerPanel（B16.1：仅过渡使用，退场记账） | `self.engine.layers = new_layers` |
| src/app/controllers/layer_controller.py:466 | 过渡 LayerPanel（B16.1：仅过渡使用，退场记账） | `self.engine.active_layer_index = min(source_active_idx, len(new_layers) - 1)` |
| src/app/controllers/layer_controller.py:583 | 过渡 LayerPanel（B16.1：仅过渡使用，退场记账） | `self.engine.layers = new_layers` |
| src/app/controllers/layer_controller.py:584 | 过渡 LayerPanel（B16.1：仅过渡使用，退场记账） | `self.engine.active_layer_index = min(source_active_idx, len(new_layers) - 1)` |
| src/features/color/adapters/color_stack_hydrator.py:251 | 投影（图→引擎 hydrate） | `engine.layers = restored` |
| src/features/color/adapters/color_stack_hydrator.py:252 | 投影（图→引擎 hydrate） | `engine.active_layer_index = 0` |
| src/features/color/adapters/color_stack_hydrator.py:301 | 投影（图→引擎 hydrate） | `engine.layers = restored` |
| src/features/color/adapters/color_stack_hydrator.py:303 | 投影（图→引擎 hydrate） | `engine.active_layer_index = (` |
| src/features/color/core/async_renderer.py:279 | 渲染快照收集（读路径聚合） | `self.layers = []` |
| src/features/color/core/async_renderer.py:1315 | 渲染快照收集（读路径聚合） | `self.layers = data['layers']` |
| src/features/color/core/image_engine.py:363 | 引擎内部状态（layers 容器本体） | `self.layers = [Layer("Layer 1")]` |
| src/features/color/core/image_engine.py:364 | 引擎内部状态（layers 容器本体） | `self.active_layer_index = 0` |
| src/features/color/core/image_engine.py:1230 | 引擎内部状态（layers 容器本体） | `self.layers.append(new_layer)` |
| src/features/color/core/image_engine.py:1231 | 引擎内部状态（layers 容器本体） | `self.active_layer_index = len(self.layers) - 1` |
| src/features/color/core/image_engine.py:1254 | 引擎内部状态（layers 容器本体） | `self.layers.pop(index)` |
| src/features/color/core/image_engine.py:1255 | 引擎内部状态（layers 容器本体） | `self.active_layer_index = max(0, min(self.active_layer_index, len(self.layers) - 1))` |
| src/features/color/core/image_engine.py:1263 | 引擎内部状态（layers 容器本体） | `self.active_layer_index = index` |
| src/features/color/core/image_engine.py:1333 | 引擎内部状态（layers 容器本体） | `self.layers.append(new_layer)` |
| src/features/color/core/image_engine.py:1334 | 引擎内部状态（layers 容器本体） | `self.active_layer_index = len(self.layers) - 1` |
| src/features/color/core/image_engine.py:4657 | 引擎内部状态（layers 容器本体） | `self.layers = HistoryManager.deserialize_layers(layers_data)` |
| src/features/color/core/image_engine.py:4670 | 引擎内部状态（layers 容器本体） | `self.active_layer_index = state.get('active_layer_index', 0)` |
| src/features/color/core/image_engine.py:4672 | 引擎内部状态（layers 容器本体） | `self.active_layer_index = max(0, len(self.layers) - 1)` |
| src/features/edit/core/edit_project_io.py:380 | 工程 IO（legacy 载荷读写） | `color_stacks[stack_id] = ColorStackRef(` |
| src/features/edit/core/edit_project_io.py:640 | 工程 IO（legacy 载荷读写） | `color_stacks[stack_id] = ColorStackData.from_dict(stack_data)` |
| src/features/edit/core/project.py:1145 | 栈权威容器（update_color_stack 单入口） | `color_stacks={},` |
| src/features/edit/core/project.py:1181 | 栈权威容器（update_color_stack 单入口） | `color_stacks={stack.stack_id: stack},` |
| src/features/edit/core/serialization.py:640 | 序列化（legacy 载荷） | `color_stacks[stack_id] = ColorStackData.from_dict(stack_data)` |
| src/shared/ai_models/swinir.py:280 | 无关同名属性（误报豁免） | `self.layers = nn.ModuleList()` |
| src/shared/ai_models/swinir.py:294 | 无关同名属性（误报豁免） | `self.layers.append(layer)` |
| src/shared/project/topos_project.py:606 | 工程 IO（topos 工程） | `color_stacks = {k: ColorStackRef.from_dict(v) for k, v in data.get("color_stacks", {}).items()}` |
| src/shared/project/topos_project.py:736 | 工程 IO（topos 工程） | `color_stacks={stack_id: color_stack},` |

**待审计：0 处**——已分类写点全部入账（迁移/投影/工程IO/过渡），B16.5 切换前据此清单退场

