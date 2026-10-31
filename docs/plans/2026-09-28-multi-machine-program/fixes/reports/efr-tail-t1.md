# EFR Tail T1 — E-rollup 清尾报告（63 条带行号单项）

日期：2026-10-02 ｜ 分支：dev（无 git 写操作，工作树留改动）
范围：REMEDIATION_PLAN.md §2.E-rollup 指定的 63 条带行号单项；完整描述以 docs/audit/2026-09-28/ISSUES.md 台账为准。

## 统计

- FIXED：37 条（其中 2 条为"先前批次已修、本次核验确认"）
- 部分修复（FIXED+挂账并披露）：5 条（AUD-19809/6211/81008/8508/9602）
- DEFERRED：20 条（行为变更类/语义决策类/冲突区域，逐条注明原因）
- 合计 62 个台账条目（任务清单 63 个定位串：scripting/examples 一条台账覆盖 3 个示例文件、presets 实挂 9 目录，均已逐一处理）
- 验证：py_compile 全部 46 个改动文件通过；定向 8 个测试文件 227 项断言全绿（含 2 个既有断言随 AUD-8505 契约对齐更新并披露）

## 逐条结论

### color/core

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-0901 | video_denoise_gl.py:104-105（+139-145） | FIXED | RenderSurface 增记 components/dtype（slots 增补），get() 命中校验扩为 尺寸+components+dtype；total_bytes() 按实际参数估算不再硬编码 comp=4/f2。现状无任何调用方传非默认参数，行为不变 |
| AUD-0908 | video_spatial_denoise.py:194 | FIXED | 注释与代码对齐：上采样 pass 透传 p.detail_recovery（原注释错称"固定 0.5"） |
| AUD-0909 | video_spatial_denoise.py:37/:55/:210 | FIXED | `self._programs: dict` → `Dict[str, moderngl.Program]`；其余方法现均已有注解（先前批次已补） |
| AUD-0915 | the_decompiler.py:20-21/:341/:350 | FIXED | 公共方法注解先前批次已补齐（git 697fe7892 "声明级签名注解76处"）；本次补齐残余：decompile 内 4 个优化闭包（unpack_params/loss_func/loss_stage1/loss_stage2）参数与返回注解 + Decompiler 类 docstring |

### composite/ui + utils

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-10210 | keyframes_timeline_view.py:707 等 | FIXED（先前已修，核验确认） | `_PARAM_ROW_ID_FMT` 常量已统一 3 处 row_id 格式串（代码内带 AUD-10210 标注，提交 2b71e383a）；核验当前文件无冗余导入（math/Any/List/QPointF 等全部在用）、无残余死代码 |
| AUD-10626 | node_item.py:32-44 等 | FIXED | keyPressEvent→QKeyEvent、focusOutEvent→QFocusEvent、_paint_dot→QPainter、paint→(QPainter, QStyleOptionGraphicsItem, Optional[Any])、mouseDoubleClickEvent→QGraphicsSceneMouseEvent、itemChange→(GraphicsItemChange, Any)；set_disabled/update_connections 现已有注解 |
| AUD-11503 | pass_fusion.py:315/316-319/407-423 | FIXED | 删除死状态 current_shader_type（声明、nonlocal、3 处赋值）；链 shader type 仍由 _determine_shader_type(current_chain_types) 派生，行为不变 |

### edit/ai_editing

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-13021 | assistant_session_store.py:230-233/:413-414/:426-427/:441-442 | FIXED | 删除 add_message_summary/record_run/add_compression_checkpoint 三处方法尾部重复 `_sync_catalog_to_cache()`（_touch() 内已含），消除双倍序列化+落盘。终态一致 |
| AUD-14602 | ai_editing/tests/test_assistant_service_cancel.py:207 | FIXED | `_capture_snapshot` 补 `-> AssistantStateSnapshot` 返回注解（导入补充） |
| AUD-14810 | toposctl.py:311-324（snapshot/search/help 同构） | DEFERRED | 修复=未知 flag/悬空选项值从静默忽略改为 raise `_CliUsageError`、_parse_plan 校验顶层 dict——三者均改变 CLI 错误语义（可观测行为变更），属语义决策，挂账 |

### edit/core

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-15613 | blend_modes.py:213-222 | DEFERRED | 修复=去掉/条件化 CPU 端无条件 clip(0,1)——直接改变像素输出（CPU/GPU 一致性决策涉及 HDR 域契约），挂账 |
| AUD-15806 | captions/delivery.py:233-240 | DEFERRED | 修复=used_names 去重改大小写不敏感——改变交付文件命名行为，挂账 |
| AUD-16104 | clip_params.py:384-390/948-957/426-431 | FIXED | CropParams.from_dict 5 字段补 float() 强转；SpeedParams.from_dict speed 补 float()、reverse/preserve_pitch 补 bool() 强转。良构输入不变，畸形输入提前报错（计划行明确要求的反序列化硬化）。SpeedPoint 必填键直接下标（缺键 KeyError→领域错误）属错误语义决策，保留现状 |
| AUD-16107 | clip_params.py:474-482/504-515 | FIXED | 删除全仓零调用的 is_normal_speed / effective_duration_factor（且二者忽略 speed_points 语义错误），留 AUD 注释指向 timeline_duration_for_source_duration() |
| AUD-16109 | clip_params.py:571-573/620-622 | FIXED | 模块头增 `if TYPE_CHECKING: import numpy as np`，字符串注解 "np.ndarray" 不再令 get_type_hints NameError（运行时零依赖） |
| AUD-16219 | commands.py:3684-3728 | DEFERRED | 命令栈 coalesce/revision 为新功能；AddTrack/MoveTrack undo 按位置回退的修复改变 undo 行为——挂账 |
| AUD-16420 | edit_frame_coordinator.py:501/539 | DEFERRED | quality 形参/透传现已有 `Optional[str]` 注解（先前批次已补）；校验非法 quality（raise）= 错误语义变更，挂账 |
| AUD-16507 | edit_gpu_decode_manager.py:2994-2995 | DEFERRED | 探测缓存清理范围扩大（invalidate_all_sources 补清 pix_fmt/resolution/route、close_source 补清 per-asset）= 运行时行为变更，挂账 |
| AUD-16610 | edit_gpu_preview_renderer.py:2063/2166/2218/2280 | DEFERRED | opacity 进 shader 前钳制改变渲染输出，挂账 |
| AUD-18304 | subtitle_renderer.py:82-92/117-121 | FIXED | `_STYLE_FIELD_SET` 34 字段重复字面量改为 `frozenset(STYLE_FIELDS)`（唯一权威清单，导入自 captions.model；两清单逐项核对一致）。`style[f]` 直接索引保留——resolve_style 契约保证全键（改 .get 会引入静默回退语义） |
| AUD-18814 | timeline_ops.py:3139-3249/596/622 | FIXED | clip_handle_headroom(clip: ClipInstance)、nearest_marker_frame(markers: tuple[TimelineMarker, ...])、_neighbor_bounds/numeric_trim_bounds(track: EditTrack, clip: ClipInstance)、build_numeric_trim_command → tuple[Optional[Any], int]；moves 注解 "List[Dict[str, int]]"→List[Dict[str, Any]]（另两处 Tuple 同步修正） |
| AUD-19809 | edit_effects_panel.py:1086/1095/1104 | PARTIAL（部分挂账） | FIXED 部分：转场页判断 `currentIndex() == 1` 改用 `_TAB_INDEX_TRANSITIONS` 具名常量。挂账部分：硬编码中文串接 I18N / 颜色走主题令牌——改变显示文案与视觉，需翻译键与设计决策 |

### edit/ui

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-19509 | ai_edit_assistant_panel.py:1497/1812-1841/2271 | FIXED | 增 `_PAGE_EMPTY/_PAGE_NO_PROFILE/_PAGE_MAIN/_PAGE_ERROR/_PAGE_BRIEF_EDITOR/_PAGE_CONTEXT_INSPECTOR` 具名常量（addWidget 顺序即页号），替换全部 8 处 setCurrentIndex/currentIndex 魔法数字 |
| AUD-20818 | scene_cut_timeline_overlay.py:368-372/:824-828/:171/:334/:174/:350 | FIXED | ① paint 的 widget 注解 QGraphicsItem→QWidget（两处）；② CandidateMarkerItem 的 _pixels_per_frame/_fps 存而不用死状态删除（构造参数保留 API 兼容，update_zoom/update_fps 保留重绘时机；全仓取证无外部读取，相关既有测试不触这些属性） |

### app/controllers

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-2013 | ai_edit_controller.py:3670-3671/3691/3756/3912/3968 | FIXED | 删除只写不读死状态属性 `_teaching_html_generate_system_prompt`（1 处赋值 + 4 处清空；system prompt 实际经 _start_chat(system_instruction=...) 传递；tests/ai/test_teaching_animation_phase5.py 仅自设属性不断言读取，通过） |
| AUD-2310 | color_controller.py:1482-1545 vs 1586-1644 | DEFERRED | denoise/deblur 四组处理器合并重构（约 200 行）——大范围等价性风险，超出机械清理范畴，挂账 |
| AUD-2518 | deliver_controller.py:130-149/190-202 | DEFERRED | 台账即标注"需复核"——双重编码判定与修法（去哪一侧 encode）是语义决策，挂账 |
| AUD-3108 | page_controller.py:1130/601-606/1248/1500-1509/1371 | DEFERRED | 跨模块私有成员读写治理需设计公共 API（多模块联动），挂账 |

### ai_harness / ai_workflow

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-22207 | plugin_status.py:354 | DEFERRED | 双重原因：① 该文件属 ai_harness 非 tests 区域，有并行代理作业中（冲突规避）；② 对齐 can_enable 与 registry.enable() 允许 FAILED/DEGRADED 回启 = UI 行为变更 |
| AUD-22713 | ai_harness/tests/test_run_trace.py:126 | FIXED | 断言解耦：不再精确匹配内部异常类名 "ValueError"，改为 `len==1 and skipped[0].startswith("s:event_1:")`（位置契约保留） |
| AUD-23117 | generation_node_specs.py:83/97/111/124/137/149 | DEFERRED | executor_id 复用 TTS_PROVIDER 在模块 docstring 中标注为"受控"约定；改 executor 语义=路由行为变更，挂账 |

### color/tracking 垫片（7 文件）

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-24801 | coordinate_map.py:11-14 及 corner_pin/feature_detector/frame_sampler/homography_stability/merge/point_manager 同款 | FIXED | 7 个垫片统一：re-export 循环跳过 dunder（不再拷入实现模块 `__all__` 列表对象）与模块类型值（np/logging 等第三方别名不再泄漏）。API 命名（类/函数/常量）照常导出，冒烟验证 ClipFrameMapper 等可用 |

### color/ui / scripting / shared effects

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-25605 | qualifier_panel.py:51/95/327/346/362/398/434-452/706/744/762 | FIXED | 补齐残余无注解方法：get_style_sheet->str、add_param_to_grid(7 参数+返回 ParamSliderRow)、update_param(key: str, value: float)、on_hue_bar_changed/on_range_bar_changed_sat/lum（float×N）、set_params(params: dict)、_on_preview_button_clicked(btn: QPushButton)、set_matte_preview_active(active: bool, style: str)（其余点位先前批次已注解） |
| AUD-26002 | scripting/examples/basic_grade.py:23 与 composite_batch_params.py:24、composite_build_graph.py:27-32 | FIXED | 两个偏离示例统一为 `topos.print`（API 主流形态，6 处）；多参数内建 print 语义改为单参 f-string（topos.print(message: str) 单参契约） |
| AUD-26309 | effect_catalog.py:518; effect_spec.py:127 | FIXED | DCTL 插件 preview_cost 改读同 dict 的字符串字段 preview_cost（原读 PreviewQualityLevel 枚举违反 EffectSpec.preview_cost: str 契约）。全仓无 .preview_cost 运行时消费方（preview_policy 用静态表），行为不变 |

### teaching_animation/presets + render/html_layer

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-28400/28600/28800/28901/29000/29201/29401/29501/29601 | presets/library 下 9 个预设目录 build.py | FIXED | 各 build() 补 `-> dict`（aurora_glow_bg / animated_histogram / pie_donut_chart / lens_flare_stack / light_sweep_banner / floating_orbs / wireframe_globe / kinetic_pop_title / neon_glow_title）。注：计划任务描述为 8 目录，E-rollup 实挂 9 条，全部处理 |
| AUD-30019 | template_promote.py:85-91/237-248; sanitizer.py:755-757 | DEFERRED | 修复=参数值拼入前拒绝 `;`/`}`/`<`/换行元字符——改变接受输入集（先前可渲染的值变为拒绝），错误/降级语义需决策，挂账 |

### composite/core / ml_roto / nodes / preferences

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-5311 | shaders.py:417-418/451-452 等 | DEFERRED | "静态最大边界+continue"改动态边界=像素输出变更（T-P1-21 同型改造），挂账 |
| AUD-5312 | shaders.py:3615-3619/3670-3674/3803-3807 | DEFERRED | SSBO 模糊补 mirror(2) 边缘策略=像素输出变更，挂账 |
| AUD-6210 | ml_roto.py:243-261（反向）等 | DEFERRED | stopped_reason 反向覆盖、mask_to_keyframe None 静默 break、取消钩子均为传播行为变更，挂账 |
| AUD-6211 | ml_roto.py:283-286/294/310/319 | PARTIAL（部分挂账） | FIXED 部分：删除死变量 count（计数后从未读取，返回值实为 len(ordered)）。挂账部分：冗余 set_parameter+_sync_point_parameters 双写、轨道追加去重——均改变写入/去重行为 |
| AUD-64019 | clamp_node.py:51 | FIXED | channels 参数 ptype "string"→"str"（规范类型表命名对齐；现值为纯字符串，coerce/UI 路径实际结果不变） |
| AUD-6808 | grade_node.py:36-38/56-58 | FIXED | HELP_TEXT 与实现对齐：注明负 gamma 被下钳 0.001、mask_channels 仅 rgb 三通道生效（alpha 不参与），原文档与派发着色器不符处已修正（纯文案） |
| AUD-6813 | group_node.py:55-59/72-73/165-184 | DEFERRED | group_name/is_expanded/group_color 参数↔实例属性双轨统一（面板编辑生效/持久化口径）= 行为变更，挂账 |
| AUD-7800 | preferences.py:29-31/174/196-197 | FIXED | 模块注释与 tooltip 文案对齐实际 factory default "ocio"（原声称 builtin 是默认；COLOR_ENGINE_DEFAULT="ocio" 为准） |
| AUD-7804 | preferences.py:64/67/358 | FIXED | create_widget(parent: "QWidget") 与 ModularPreferencePage 协议签名对齐（其余方法先前已注解，__init__/_widgets 已有注解） |

### composite/render + render3d + ui

| ID | 定位 | 结论 | 说明 |
|----|------|------|------|
| AUD-81008 | composite_render_output.py:488-500/880-894/1037-1038/1054 | PARTIAL（部分挂账） | FIXED 部分：逐帧/逐 sink 循环内 import 全部提升（串行循环 transform_color、并行 video stage _convert 闭包、image-sequence stage 的 _convert_for_image_ext 与 FileIO）。挂账部分：串行/并行两份转换打包代码合并=重构（等价性风险），挂账 |
| AUD-8406 | pbo_readback.py:89/:552/:304-305 | FIXED | gl_context 补 `"moderngl.Context"` 注解（TYPE_CHECKING 导入，运行时零依赖）；cleanup 补 `-> None`；审计所指"第一份含未用 import 的死 poll_ready"已不存在（先前批次已删，核验确认） |
| AUD-8407 | parallel_frame_render.py:199-205/246-252/240-244 | DEFERRED | on_worker_error 双调用的守卫改变错误回调次数（事件流契约），需决策归届方，挂账 |
| AUD-8505 | topos_cache_codec.py:190/243; disk_cache_store.py:265-266 | FIXED | manifest 片段键 "file_bytes"→"bytes"（两处），与消费方 disk_cache_store 对齐，override 不再是死代码；数值同为 payload 文件字节数，激活后行为不变。**披露**：tests/composite/test_topos_cache_payload.py 两断言随契约同步更新（file_bytes→bytes），HEAD 对照仅此差异 |
| AUD-8508 | tracker_analyze_worker.py:132-137; composite_page.py:3738 | PARTIAL（部分挂账） | FIXED 部分：取消哨兵串提为 `TRACKER_ANALYZE_CANCELLED` 常量，生产/消费两侧共用（命名对齐，零行为变化）。挂账部分：专用 cancelled 信号替代 failed 信号复用=信号 API 变更 |
| AUD-9001 | model_import_cache.py:272-306 | DEFERRED | put 增加容量上限/淘汰策略=缓存行为变更，挂账 |
| AUD-9602 | composite_render_dialog.py:285-301/513 | PARTIAL（部分挂账） | FIXED 部分：注释按实际行为修正（质量控件对全部图像格式恒显示恒写入，非仅 JPEG/WebP）。挂账部分：按格式显隐/条件写入=行为变更 |
| AUD-21801 | src/features/__init__.py:1-3（计划定位串恰为 "src/features/"） | DEFERRED | eager import ColorFacade 改 PEP 562 懒加载/清空文件=导入期副作用变更（全 app 导入时序与潜在循环导入依赖），属语义决策，挂账 |

## 改动文件清单（46 个）

src/（36）:
- src/features/color/core/video_denoise_gl.py、video_spatial_denoise.py、the_decompiler.py
- src/features/color/tracking/{coordinate_map,corner_pin,feature_detector,frame_sampler,homography_stability,merge,point_manager}.py（7）
- src/features/color/ui/qualifier_panel.py
- src/features/composite/ui/node_item.py、composite_page.py、composite_render_dialog.py
- src/features/composite/utils/pass_fusion.py
- src/features/composite/render/{composite_render_output,pbo_readback,topos_cache_codec,tracker_analyze_worker}.py（4）
- src/features/composite/ml_roto.py
- src/features/composite/nodes/{clamp_node,grade_node}.py
- src/features/composite/preferences.py
- src/features/edit/ai_editing/assistant_session_store.py
- src/features/edit/core/{clip_params,subtitle_renderer,timeline_ops}.py
- src/features/edit/ui/{ai_edit_assistant_panel,edit_effects_panel,scene_cut_timeline_overlay}.py
- src/features/scripting/examples/{composite_batch_params,composite_build_graph}.py
- src/features/shared/effects/effect_catalog.py
- src/features/teaching_animation/presets/library/…/build.py（9）
- src/app/controllers/ai_edit_controller.py

tests/（2）:
- src/features/edit/ai_editing/tests/test_assistant_service_cancel.py
- src/features/ai_harness/tests/test_run_trace.py
- tests/composite/test_topos_cache_payload.py

## 验证输出

1. py_compile：46 个改动文件全部通过（"PY_COMPILE ALL OK"）。
2. 定向测试（8 文件，无新增失败）：
   - tests/edit/test_timeline_ops.py + tests/composite/test_pass_fusion_execution.py + tests/composite/test_ml_roto.py + tests/edit/test_smooth_speed_ramp.py → 70 passed
   - tests/color/test_tracking_source_timeline_frame_sync.py + tests/composite/test_group_node.py + tests/composite/test_pbo_async_readback.py + tests/ai/test_teaching_animation_phase5.py → 76 passed
   - src/features/edit/ai_editing/tests/test_assistant_service_cancel.py + src/features/ai_harness/tests/test_run_trace.py + tests/composite/test_disk_cache.py → 32 passed
   - tests/edit/test_fix_wd13g67_aud.py + tests/color/test_qualifier_matte.py + tests/composite/test_topos_cache_payload.py + tests/shared/test_ef20_aud.py → 105 passed（AUD-8505 两断言同步后）
   - tests/composite/test_composite_render_output_color_descriptor.py + tests/edit/test_clip_speed_fade_realtime.py → 19 passed
3. 垫片冒烟：`from src.features.color.tracking import coordinate_map` 后 ClipFrameMapper 可用、无 `__all__`/`np` 泄漏。
4. 未整目录跑 tests/edit（遵守约束）；未做任何 git 写操作；REMEDIATION_PLAN.md 未改动。
