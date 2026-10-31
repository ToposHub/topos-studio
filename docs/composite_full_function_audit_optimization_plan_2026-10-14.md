# 合成页面全面功能检查与优化计划表（2026-10-14）

状态：审计与计划已完成；最后续验2026-10-16。产品缺陷尚未修复。本次新增的是计划和证据，没有修改产品源码或测试素材。

## §0 实施状态索引（2026-10-16 B0 批次）

B0（保存与状态安全）已实施并通过定向回归，状态为“定向回归通过”（V-01 真实窗口旅程与全套隔离复跑待做）：

| 编号 | 状态 | 实施位置 / 回归 |
|---|---|---|
| U-01 | 定向回归通过 | `src/shared/project/composition_merge.py`（新，GRAPH_OWNED_KEYS 含 layers/layer_stacks）；`composite_controller._sync_composite_graph_to_edit_project` 改用共享 merge；tests/composite/test_composite_b0_save_session_fixes.py |
| U-02 | 定向回归通过 | `composite_page.reset_composite_session` 完整会话重置（清 UndoStack、取消 pending、停 timer）；同上测试文件 |
| U-03 | 定向回归通过 | `_on_parameters_changed_batch` 归属校验+batch/single 互斥 flush；`_undo_batch_param` 按键存在精确恢复 None |
| U-04 | 定向回归通过 | `_commit_pending_edit_transactions`；undo()/redo() 入口先提交 pending |
| U-05 | 定向回归通过 | `load_composite_data` 先验证 nodes 字段；合法空图走完整清图+元数据恢复；非法 payload 保持当前图 |
| U-09 | 定向回归通过 | `node_editor_widget.reset_session_state`（锁/导航/选中）；page 会话切换调用 |
| U-11 | 定向回归通过 | `save_router._save_composite_to_edit_project` 改用共享 merge（保留来源/身份/扩展字段）；runtime/controller graph 回退走 `page.get_composite_data()` |
| U-12 | 定向回归通过 | `composition_merge.merge_viewer_state` 按键合并（图编辑器键+显示键+未知键） |
| U-13 | 定向回归通过 | `reset_session_state` 清 group_view_stack/filter 并发 root 信号 |
| N-01 | 定向回归通过 | `node_engine.set_node_expression` 提交前捕获表达式/启用状态/关键帧轨道，环检测失败完整回滚 |
| QA-01 | 完成 | `scripts/run_composite_tests.sh` 导出 OPENCV_IO_ENABLE_OPENEXR=1 + EXR roundtrip 探针；test_ai_remove_wiring 18/18 |
| QA-02 | 完成 | test_fix_nodes1_aud2.py fake-GL 类 mock `_query_draw_fbo_binding`/`_restore_draw_fbo_binding`；12/12 无 SIGSEGV |

既有失败（非本批次引入，干净树同样失败）：`tests/app/test_gap002_edit_controller.py::test_timeline_settings_requested_updates_refs_and_sequences`（fps Rational 24!=25，Edit 时间线域）。

## §0.1 实施状态索引（2026-10-16 B1 批次）

B1（图编辑与动画语义）的 U 系列已实施并通过定向回归；B1 剩余 N-02/03/04/05/12（表达式/参数求值语义）尚未开始：

| 编号 | 状态 | 实施位置 / 回归 |
|---|---|---|
| U-06 | 定向回归通过 | `load_composite_data` 装饰恢复后重建 add_backdrop_visual/add_sticky_note_visual + containment 刷新；state/装饰字典深拷贝（修复 clear() 经共享引用清空调用方 payload 的别名缺陷）；tests/composite/test_composite_b1_edit_semantics_fixes.py |
| U-07 | 定向回归通过 | 页面统一 `_push_node_enabled_undo`（单节点/属性面板）+ `nodes_bypass_batch_toggled` 批量单条 group undo |
| U-08 | 定向回归通过 | `_apply_project_settings` before/after 快照形成单个 ProjectSettings 命令，undo/redo 走 `_restore_project_settings_state` |
| U-10 | 定向回归通过 | `_get_upstream_nodes`/`_get_downstream_nodes` 改迭代 BFS（旧预标记导致只遍历一跳），仅返回图中节点 |
| U-14 | 定向回归通过 | `_on_nodes_connected` 提交前捕获旧边；`_push_replace_connection_undo` 原子替换命令；重复连接不生成历史 |
| U-15 | 定向回归通过 | 新增 `remove_connection_visual_exact`（quad 精确删除）；断线/undo/redo 全部改精确删除；`disconnect_by_dst` 不再自行删视觉（页面是唯一删除方） |
| U-16 | 定向回归通过 | `nodes_pasting` 静默装配事务：粘贴/预设内部连线不单独入历史，`nodes_pasted` 单组记录（try/finally 退出静默态） |
| N-02 | 定向回归通过 | `ExpressionManager.set_expression` 接受 `local_param_names`：this.foo 注册为同节点图边，提交/快照/add_node/from_dict 四路径统一；求值顺序不再依赖插入顺序；tests/composite/test_composite_b1_expression_semantics_fixes.py |
| N-03 | 定向回归通过 | `node_engine._resolve_expression_node_ref` canonical 化（ID/n_别名/显示名）；manager 同时保留 raw 别名边（快照值发布）与 canonical 边（环检测），跨别名环提交即拒 |
| N-12 | 定向回归通过 | `_numeric_truth` 统一 nonzero 真值；BoolOp 逐项短路、IfExp 负数条件为真、Compare 链短路 |

## §0.2 实施状态索引（2026-10-16 B2 批次）

B2（可靠渲染与交付）的 R-01～R-13 已实施并通过定向回归（真实生产 pool/PyAV 复现脚本逐项复核）；R-15/R-22/R-23/R-24 及 RG 项未开始：

| 编号 | 状态 | 实施位置 / 回归 |
|---|---|---|
| R-01 | 定向回归通过 | `gpu_resource_pool.upload_pooled_texture`（按目标 dtype 转换、失败归还纹理）；Paint result/mask 与 AI `_upload_ai_result_texture` 改用；tests/composite/test_composite_b2_render_delivery_fixes.py |
| R-02 | 定向回归通过 | Paint straight-alpha 只预乘 RGB，alpha 保留（真实 8×8 生产 pool 读回 [0.1,0.1,0.1,0.5]） |
| R-03 | 定向回归通过 | `_rgb_intermediate_descriptor`：RGB 中间值 full range，limited 压缩唯一发生在 YUV 打包层（黑/白 Y=16/235 实测）；串行+并行+占位路径统一 |
| R-04 | 定向回归通过 | BT.2020 numpy 精确转换前 BGR→RGB 换序（红 Y=294 实测，旧 116） |
| R-05 | 定向回归通过 | 图片序列原色矩阵 BGR 换序转换（P3 红 BGR16=[9080,13125,60127] 与审计期望一致），alpha 通道保留 |
| R-06 | 定向回归通过 | `_pix_fmt_bit_depth` + `_pack_encoded_rgb`：位深由选定 pix_fmt 决定，wide-gamut SDR 10-bit 升级后 uint16/bgr48le 提交 |
| R-07 | 定向回归通过 | 编码元数据一律从 descriptor 构造（像素与标签同源）；仅标准 HDR10 附 ST 2086/MaxCLL，P3+PQ 等自定义组合不再被固定标签覆盖 |
| R-08 | 定向回归通过 | `RenderOutputResult.status` complete/cancelled/partial/failed；中途取消已写帧=partial（保留成果不报成功）；AUD-81001 四个"取消即成功"契约测试更新 |
| R-09 | 定向回归通过 | 全部帧渲染失败 → failed + 删除成果（旧：全黑视频 success=True） |
| R-10 | 定向回归通过 | 序列渲染 None/写盘失败计数入 frames_dropped；complete 必须 written==total |
| R-11 | 定向回归通过 | 串行图片序列 try/finally `_cleanup_frame_renderer`（完成/取消/失败/异常全出口） |
| R-12 | 定向回归通过 | 并行序列 stage 接入 factory_fallback 回调，全 worker 创建失败回退串行 |
| R-13 | 定向回归通过 | `_apply_project_motion_blur_shutter` 存 pending，`_install_eval_context` 重建 ctx 继承；multi-pass 入口补齐注入（0.9 快门实测生效） |

既有失败（非本批引入，干净树同败）：`test_gap002_edit_controller`（fps 断言）、`tests/export/test_gpu_encode_surface.py::test_surface_cannot_submit_twice`（错误文案不匹配）。

## §0.3 实施状态索引（2026-10-16 B3 批次）

B3（时间与数据通道）已实施并通过定向回归。N-06/N-07 完成的是计划允许的短期方案（参数维度冻结 + 冲突显式化）；完整 (node,frame,port) 按分支求值与 image 序列/timeline 源的 override 消费仍属后续架构项。RG-08（标准 Cryptomatte 多层 EXR writer）未开始。

| 编号 | 状态 | 实施位置 / 回归 |
|---|---|---|
| N-04 | 定向回归通过 | `base_node.set_expression`/`set_keyframe_track` 不再互删（base+覆盖政策）；from_dict 保留两者；`ExpressionPolicy` 默认共存的；`loop_value` 范围内帧直接采样轨道；0/12/24/36→[0,12,24,12]、禁用回 base、valueAtTime(0.5)=12 实测；tests/composite/test_composite_b3_time_data_fixes.py |
| N-05 | 定向回归通过 | `NodeSnapshot.parameters_resolved` 标记；`_create_ephemeral_node` 采用已解析参数（父 x=100+子轨道帧5=105 不再被覆盖回 5；表达式结果不被重插值） |
| N-06（短期） | 定向回归通过 | `snapshot()` 重排：frame_overrides 先于参数求值，按节点有效帧分组求值——FrameHold(5) 下游帧 20 的上游动画参数取 5；源类型扩展（image 序列/timeline）待 (node,frame) 模型 |
| N-07（短期） | 定向回归通过 | `compute_frame_overrides` 接入 conflict_logger，`GraphSnapshot.time_conflicts` 携带可定位诊断（不再静默 first-wins）；按分支求值待后续 |
| N-08 | 定向回归通过 | 非整数速度保留 inner 链逐级 apply（0.5→2.0 帧 1/3/5 顺序=合并=[0,4,4]）；整数速度仍精确折叠 |
| N-09 | 定向回归通过 | 选区身份改 uint32 位模式（`_id_key`，增删/toggle/dedup/name_for_id 全链）；GLSL float_r 路径 `floatBitsToUint` 位相等（rgba24 保留整数容差） |
| N-10 | 定向回归通过 | `uint32_to_safe_float`（指数压缩 [1,254]、清符号）：10,000 对象名全部有限且唯一；remap/name_for_id 与 manifest/AOV 同一转换；JSON allow_nan=False |
| N-11 | 定向回归通过 | `_decode_json_list`/manifest dict 校验：null/数字/数组/字符串结构返回空选区不抛异常；remove_selection 按 zip 位置配对 |
| R-23 | 定向回归通过 | 数据 AOV 含 [0,1] 外数值写入 png/jpg/tif 时显式 ValueError（引导 EXR）；EXR 逐值保留；in-range 数据通道不受影响 |

契约测试更新（旧行为→新政策）：test_composite_keyframes_serialization（A-15 互斥→N-04 共存）、test_timeline_expression（默认政策）、test_timeline_commands/wd4g27（表达式记录不再含删除轨道）。

## §0.4 实施状态索引（2026-10-16 B4 批次——部分实施）

B4 本会话完成可离线验证的四项；材质簇（R-16~19/R-20）、能力补齐（NG-01~06、RG-01/02/03/04/06/08、C-01）与 R-24 的完整异步关窗状态机未开始：

| 编号 | 状态 | 实施位置 / 回归 |
|---|---|---|
| R-14 | 定向回归通过 | `GraphSnapshot.global_3d_scene_settings`（viewer_state.default_3d_scene 随快照）；`CompositeExportRenderer._apply_global_3d_scene_settings` 与 R-13 同一 pending 模式（含 ctx 重建继承），readback/multi-pass 入口安装——导出不再回落 Scene3D 旧默认；tests/composite/test_composite_b4_3d_registry_fixes.py |
| R-21 | 定向回归通过 | `_resolved_file_stat_signature`（纳秒 mtime+size）进内容签名——同路径同秒同尺寸覆盖触发重载（load 计数 1→2 实测）；缺文件签名必然失配 |
| N-13 | 定向回归通过 | `NodePluginManager.on_registry_sync` 钩子 + `node_engine.sync_plugin_node_registry`（增量同步 NODE_REGISTRY/NODE_CATEGORIES；重载换类；卸载后 create_node_by_type 返回 None） |
| R-24（部分：传播合同） | 定向回归通过（实机关窗验收待 V-06） | `CompositePage._renderer_stop_result`/`pipeline_shutdown_complete()`；`MainWindow._shutdown_playback_pipeline` 汇总 owner 结果——渲染线程超时未停时 done 保持 False（可重试）并记录未完成 owner，不再先行永久标记 done；异步关窗状态机/线程栈抓取为后续步骤 1/3/4 |

## §0.5 实施状态索引（2026-10-16 B5 批次——部分实施）

| 编号 | 状态 | 实施位置 / 回归 |
|---|---|---|
| N-14 | 定向回归通过 | ① `base_node.compiled_eval_tracks`：按 param_revision 缓存已编译 KeyframeTrack（每帧只二分采样）；② 参数浅拷贝（仅容器深拷）；③ 别名 deepcopy 只为表达式实际引用的节点构建（`_expression_alias_targets`）；④ 快照轨道/meta 序列化按 revision 缓存（`_build_snapshot_track_meta`）。实测 50n×5×120（30k 键）快照中位 118.6ms→1.9ms、100n（60k 键）237.4ms→3.9ms，均低于计划 ≤10ms 目标；缓存失效（轨道编辑/删除）验证；tests/composite/test_composite_b5_perf_repro_fixes.py |
| QA-03 | 完成（脚本侧） | bench_composite_playback/scrub 以 `__file__` 定位本 checkout（不再硬编码 topos-color01）+ `TC_BENCH_MEDIA_ROOT` 覆盖；gpu coverage 视频目录改为 env/本 checkout 默认（不再指向 Windows 桌面）；real_topos 全节点矩阵输出目录带运行日期、素材缺失显式 blocked。HEAD/dirty/设备指纹进 JSON 等 QA-03 剩余细项与 P-01~05 阶段归因/素材基准未包含 |

P-01~P-05（解码/seek/导出吞吐/诊断归因/HEVC 退速）需要真实素材的专项基准与现场归因，V-01~V-07 实机验收依赖真实窗口操作，均不在本离线批次内。

检查基线：`171ce530bf9ab8faeb436950cc9ba0eb50a3b208`。项目根目录：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02`。范围覆盖合成页面的节点编辑、图层、关键帧/表达式、查看器、源素材/时间处理、2D效果、3D材质/场景、GPU缓存/资源、保存与Edit/Deliver集成、图片/序列/视频导出。

**主要结论：54项核心问题与性能项（U 16项、N 14项、R 24项），另外列出能力不足、基准质量和待实机验收任务。** 这些不是54个全都已用真实窗口复现的bug：下面逐项注明运行证据、注入失败证据、静态调用链与性能测量。高优先项是工程信息丢失、跨合成撤销、表达式失败丢动画、错误颜色/alpha输出和不完整成果误报成功。正常路径大量测试通过，不能覆盖这些边界错误。

## 检查方法、覆盖与边界

- 读取`.clinerules`和AGENTS工作流；三路Explore/Review分别审查UI/持久化、节点/图模型、渲染/导出/3D，最后合并重复主题与核对证据。旧计划只用于背景，不作为当前bug成立依据。
- 当前合成模块有300个Python文件，注册表99个键；包含别名、兼容键和可选延迟注册，并非99个独立可用效果。参数/端口清单见`docs/audit/2026-10-14-composite/evidence/node_inventory.json`。
- 322个合成测试文件，加34个相关app/UI/project/edit文件，共356个文件逐文件独立进程运行。环境为Qt offscreen、standalone GL、BLAS单线程，避免一个native崩溃吞掉整个套件。
- 用户35个2K/4K视频全部做ffprobe，覆盖H.264、10bit HEVC、DNxHR 10/12bit、ProRes各档与TOPOS各档。通过生产CompositeFrameRenderer测23次请求/来源（3次预热/冷启动、12次顺序、4次seek、4次linear_f32导出），另测4个Grade→Gaussian Blur(24px)→Merge复杂图，合计39个场景。
- 定向复现使用真实模型/page/SaveRouter处理入口；GPU上传缺陷使用真实pool和8×8独立GL线程；编码色彩使用真实numpy/PyAV转换。失败/取消场景会以fake保存器/编码器注入，避免覆盖用户工程或写无用大文件。
- 没有逐按钮完成所有原生窗口操作、真实HDR屏幕观感、外部付费AI提供商、所有FBX/glTF/EXR/AOV组合与跨平台验证。代码/模型复现不能等同鼠标交互全部通过；这些明确进入V系列及各项验收标准。真实应用播放的短测结果与关闭状态单独记录，不能凭signal频率声称所有交互完成验收。

## 自动测试结果（2026-10-14原始全套）

| 项目 | 结果 | 解释 |
|---|---|---|
| 隔离执行文件 | 356/356 | 354文件退出0；1文件4个EXR环境失败；1文件native SIGSEGV |
| 可获取XML统计的用例 | 7711 | native崩溃文件没有完整XML，未运行完的用例没有计入“通过” |
| 原始结果 | 7679通过 / 4失败 / 28跳过 | 原始日志/XML保留，没有用复测覆盖原记录 |
| EXR启动条件复测 | 18/18通过 | 打开OPENCV_IO_ENABLE_OPENEXR后原4失败消失；按启动环境问题QA-01归类 |
| 校正启动条件后的可计数结果 | 7683通过 / 28跳过 | 仍有1个崩溃文件未完整验证，不能写“全套全过” |
| native崩溃 | test_fix_nodes1_aud2.py，returncode=-11 | fake GL测试漏mock原生FBO查询，见QA-02；不能推断真实应用同样必崩 |
| 本次新发现 | 独立复现与审查发现 | 不用现有测试通过率代替功能正确性结论 |

跳过项包括不可用Metal后端、4.3 compute与测试明确的环境限制；原始XML有逐项原因。统计把pytest标记的skip/xfail统一保留为“跳过”，不算通过。日志与环境见`docs/audit/2026-10-14-composite/evidence/`；运行耗时合计约11.5分钟。

## 测试环境

macOS15.3.1，Intel i9-9900K，32GiB RAM，AMD Radeon RX6900XT，OpenGL4.1；Python3.12、numpy1.26.4、ModernGL5.12、PyAV17.0.1、Qt6.10.0（低于pyproject当前声明的6.10.2下限，升级构建仍需复验）。本机GL没有4.3 compute能力，片元/可用native路径结果不能直接推广到其他平台。完整版本和设备参数见environment.json。

## 真实素材性能测量

所有数字单位ms。顺序预览12帧、seek4次、导出4帧，样本很短，nearest-rank p95会等于最大值；用于发现瓶颈，不能替代100次seek/30分钟播放验收。预览是离线同步求值+GPU读回，不是GUI实际显示FPS；export是linear_f32渲染+读回，没有包含视频编码/写盘。采样RSS是CPU进程内存，不是VRAM。

| 素材 / 场景 | 顺序预览中位 / p95 | Seek中位 / 最大 | linear_f32导出中位 | 采样RSS峰值MiB | 结果 |
|---|---:|---:|---:|---:|---|
| 2K_DNxHR 444 10bit.mov / baseline | 71.4 / 81.5 | 81.6 / 84.1 | 97.8 | 754.3 | 23请求有效 |
| 2K_DNxHR 444 12bit.mov / baseline | 74.0 / 85.5 | 84.1 / 94.2 | 99.5 | 758.6 | 23请求有效 |
| 2K_DNxHR HQX 10bit.mov / baseline | 69.8 / 73.6 | 81.8 / 95.8 | 101.5 | 749.1 | 23请求有效 |
| 2K_DNxHR HQX 12bit.mov / baseline | 71.3 / 84.5 | 68.6 / 76.8 | 96.0 | 738.5 | 23请求有效 |
| 2K_H265.mov / grade_blur_merge | 11.1 / 25.5 | 96.4 / 146.7 | 55.1 | 948.1 | 23请求有效 |
| 2K_H265.mov / baseline | 8.0 / 20.4 | 100.6 / 171.7 | 54.6 | 955.9 | 23请求有效 |
| 2K_h264.mp4 / baseline | 10.6 / 12.2 | 104.3 / 133.8 | 51.5 | 856.2 | 23请求有效 |
| 2K_prores422.mov / baseline | 11.8 / 13.3 | 50.5 / 134.7 | 54.5 | 743.5 | 23请求有效 |
| 2K_prores422HQ.mov / baseline | 13.1 / 14.6 | 43.6 / 133.2 | 45.1 | 775.0 | 23请求有效 |
| 2K_prores422LT.mov / baseline | 11.1 / 16.4 | 15.8 / 135.6 | 46.7 | 743.9 | 23请求有效 |
| 2K_prores422Proxy.mov / baseline | 10.6 / 22.0 | 43.7 / 130.0 | 46.1 | 722.9 | 23请求有效 |
| 2K_prores4444.mov / baseline | 73.6 / 77.7 | 79.2 / 101.2 | 98.4 | 727.8 | 23请求有效 |
| 2K_prores4444XQ.mov / baseline | 74.7 / 79.8 | 75.7 / 80.7 | 99.4 | 754.4 | 23请求有效 |
| 2K_topos_422.mov / baseline | 200.4 / 256.8 | 228.6 / 246.5 | 232.3 | 1225.2 | 23请求有效 |
| 2K_topos_422_HQ.mov / baseline | 199.6 / 239.6 | 201.2 / 220.5 | 232.0 | 1232.0 | 23请求有效 |
| 2K_topos_422_LT.mov / baseline | 199.2 / 236.9 | 203.1 / 206.0 | 236.1 | 1201.9 | 23请求有效 |
| 2K_topos_422_Proxy.mov / baseline | 197.4 / 226.7 | 194.7 / 203.0 | 219.4 | 1222.3 | 23请求有效 |
| 2K_topos_4444.mov / baseline | 192.5 / 230.7 | 183.6 / 192.1 | 203.0 | 1204.6 | 23请求有效 |
| 2K_topos_4444_XQ.mov / baseline | 179.3 / 195.9 | 185.6 / 191.3 | 206.0 | 1145.7 | 23请求有效 |
| 4K_DNxHR 444 10bit.mov / baseline | 279.4 / 313.8 | 282.9 / 293.7 | 668.9 | 2494.7 | 23请求有效 |
| 4K_DNxHR 444 12bit.mov / baseline | 267.7 / 288.9 | 283.5 / 315.2 | 666.4 | 2308.8 | 23请求有效 |
| 4K_DNxHR HQX 10bit.mov / baseline | 254.4 / 270.7 | 266.5 / 289.0 | 697.7 | 2261.6 | 23请求有效 |
| 4K_DNxHR HQX 12bit.mov / baseline | 267.0 / 295.5 | 272.1 / 277.0 | 669.4 | 2324.1 | 23请求有效 |
| 4K_H264.mov / baseline | 22.0 / 47.9 | 112.1 / 565.0 | 497.1 | 2184.8 | 23请求有效 |
| 4K_H265.mov / grade_blur_merge | 28.6 / 69.4 | 165.3 / 1237.4 | 767.0 | 2885.1 | 23请求有效 |
| 4K_H265.mov / baseline | 18.3 / 59.1 | 163.2 / 1361.1 | 489.4 | 2829.8 | 23请求有效 |
| 4K_prores422HQ.mov / baseline | 46.4 / 75.1 | 95.9 / 1390.8 | 512.6 | 2282.4 | 23请求有效 |
| 4K_prores422LT.mov / baseline | 28.7 / 39.3 | 42.3 / 924.1 | 457.2 | 2028.8 | 23请求有效 |
| 4K_prores422Proxy.mov / baseline | 28.6 / 49.9 | 35.9 / 821.6 | 467.2 | 1955.3 | 23请求有效 |
| 4K_prores4444.mov / grade_blur_merge | 269.5 / 279.0 | 285.5 / 287.4 | 893.8 | 2451.8 | 23请求有效 |
| 4K_prores4444.mov / baseline | 273.7 / 311.2 | 279.5 / 287.4 | 652.4 | 2310.7 | 23请求有效 |
| 4K_prores4444XQ.mov / baseline | 276.1 / 315.5 | 291.0 / 294.4 | 681.8 | 2305.5 | 23请求有效 |
| 4K_topos_422.mov / baseline | 734.2 / 779.1 | 743.7 / 838.8 | 883.2 | 5001.9 | 23请求有效 |
| 4K_topos_422_HQ.mov / baseline | 727.4 / 793.6 | 726.1 / 776.8 | 1100.8 | 5224.2 | 23请求有效 |
| 4K_topos_422_LT.mov / baseline | 729.4 / 741.4 | 724.0 / 870.0 | 1142.5 | 5393.6 | 23请求有效 |
| 4K_topos_422_Proxy.mov / grade_blur_merge | 726.9 / 749.0 | 718.8 / 870.7 | 1334.2 | 5447.1 | 23请求有效 |
| 4K_topos_422_Proxy.mov / baseline | 733.8 / 813.4 | 719.7 / 930.1 | 1180.0 | 5354.5 | 23请求有效 |
| 4K_topos_4444.mov / baseline | 671.7 / 744.1 | 683.1 / 709.4 | 1044.0 | 5515.4 | 23请求有效 |
| 4K_topos_4444_XQ.mov / baseline | 665.8 / 740.0 | 677.1 / 706.7 | 1096.8 | 5196.8 | 23请求有效 |

CPU快照独占复测（合成Transform图、2次预热+10次记录，不含解码/GPU）：

| 节点 | 动画轨道 | 每轨关键帧 | 表达式 | snapshot中位ms | 最大ms |
|---:|---:|---:|---|---:|---:|
| 10 | 0 | 0 | False | 0.49 | 0.538 |
| 50 | 0 | 0 | False | 2.245 | 2.511 |
| 50 | 250 | 120 | False | 120.893 | 125.303 |
| 50 | 250 | 120 | True | 129.295 | 140.438 |
| 100 | 500 | 120 | False | 237.402 | 252.049 |

N-14的最终依据以上述独占复测为准；子报告的并发初测只保留作过程记录。大量关键帧每帧深拷贝/重复解析发生在UI snapshot路径，应先使用不可变轨道和revision缓存，再保证子帧/父变换语义完整，目标snapshot p95≤5ms，不能仅优化GPU。

真实应用驱动结果（独立临时配置/测试工程；1920×1080合成、24fps；请求15秒）：

| 素材 | 驱动退出状态 | 关键输出 |
|---|---|---|
| 2K_H265.mov | 0 | mode=page duration=15.0s still_playing_at_end=True；viewer emissions: 360  rate=24.0/s；viewer max gap: 0.15s @t=2.6s；playhead stalls > 1s (while playing): 0 []；[DRIVER] audit teardown: composite_renderer_running=False window_visible=False |
| 4K_H265.mov | 2 | mode=page duration=15.0s still_playing_at_end=True；viewer emissions: 224  rate=14.9/s；viewer max gap: 0.18s @t=0.2s；playhead stalls > 1s (while playing): 0 []；[DRIVER] audit teardown: composite_renderer_running=True window_visible=False |

该驱动测composite_frame_ready信号边界与播放头，不是物理显示器呈现。driver失败或超时只能作为未完成验收记录；不能借无头渲染的吞吐推断GUI可实时。详见*.real_app.log及V系列；已经完成播放但正常关闭失败的具体合同错误见R-24。

2026-10-16真实窗口续验：每例独立临时config、起始页Color，1920×1080合成、24fps、4K来源、15秒。

| 来源 | frame_ready信号率 | 超过1秒间隙 | 正常关闭验证 |
|---|---:|---|---|
| 4K_H265.mov | 14.0 /s | 0 | exit=2；renderer=True, visible=False |
| 4K_topos_422_Proxy.mov | 23.9 /s | 0 | exit=0；renderer=False, visible=False |

TOPOS在本次窗口路径达到约23.9次信号/秒且关闭成功；不能把离线4K完整读回约733.8ms/帧解释成所有TOPOS界面播放均慢。窗口合成输出1080p、离线输出4K，且生产异步呈现与standalone读回不同，不能直接计算加速比。P-01优先检查离线路径的decode/backend、CPU转换与读回，P-05单列HEVC窗口中后段退速。信号率仍不等同物理显示器帧率或逐帧像素正确性。

## 问题索引与计划状态

所有产品修复与能力补齐项当前均“待修复/待验收”；审计副本本身的计数器/临时配置/关闭修补已经执行，QA系列仍要求同步修复正式基准入口。P1为优先阻止数据丢失/错误成果或主要功能无效，P2为后续可靠性/性能与能力补齐，P3为较低优先体验/文档。工时不是承诺日期。RG-03与NG-05的volume主题合并排期，RG-03还覆盖pointcloud，避免重复建设。

| 编号 | 优先级 | 问题 | 证据 | 工时 / 依赖 |
|---|---|---|---|---|
| U-01 | P1 | 图层/图层栈没有同步回 EditProject | 生产方法独立复现 | 4–8 h；依赖保存契约 U-11 |
| U-02 | P1 | 切合成后共享旧撤销历史 | 真实 page/stack 独立复现 | 8–16 h；与 U-05/09/13 共用 session reset |
| U-03 | P1 | 批量参数防抖混用原值，undo 写坏另一参数 | 真实 page/stack 独立复现 | 4–8 h；与 U-04 共用事务边界 |
| U-04 | P2 | 待提交参数还未入历史时 Ctrl+Z 撤错步骤 | 生产方法即时顺序独立复现，实机时序未测 | 4–8 h；依赖 U-03，计入统一事务修复 |
| U-05 | P1 | 空图载入保留上一合成节点和格式 | 真实 loader 独立复现 | 4–8 h；与 U-02 共用 loader/session reset |
| U-06 | P2 | 保存的 Backdrop/便签载入后没有视觉对象 | 真实 loader 独立复现 | 4–8 h；与 U-05/09/13 共用恢复入口 |
| U-07 | P2 | 节点旁路/启用禁用不能撤销 | 真实 action/stack 独立复现 | 4–8 h；独立命令，复用 UndoStack |
| U-08 | P2 | Project Settings 修改不能撤销 | 真实 slot/stack 独立复现 | 8–16 h；依赖格式元数据提交契约 |
| U-09 | P2 | 新合成继承上一合成节点锁 | 真实 editor/loader 独立复现 | 2–4 h；可并入 U-02/06，勿重复计时 |
| U-10 | P2 | 选择上游/下游只遍历一跳 | 4 节点真实图独立复现 | 1–3 h；独立，可先修 |
| U-11 | P1 | 合成页保存覆盖来源/身份/扩展元信息 | 真实 SaveRouter，捕获落盘前对象 | 8–16 h；第一优先，包含持久化集成回归 |
| U-12 | P1 | 保存查看器状态覆盖节点编辑器/输出目标状态 | 同上 | 4–8 h；依赖 U-11，分 schema 迁移另估 |
| U-13 | P1 | 旧分组视图过滤让新合成节点全部隐藏 | 真实 group view/loader 独立复现 | 2–4 h；可并入 U-02/06，勿重复计时 |
| U-14 | P1 | 替换单输入连线后 undo 不恢复旧线 | 真实 action/graph/stack 独立复现 | 8–16 h；连线事务基础，先于 U-15/16 |
| U-15 | P1 | 多输入断线误删其他视觉线，产生幽灵线 | 真实 Scene3D 多输入独立复现 | 8–16 h；依赖 U-14，包含多输入精确删除 |
| U-16 | P2 | 粘贴已有连接图生成多余的 Connect 历史 | 真实 clipboard/action/stack 独立复现 | 4–8 h；依赖连线事务 U-14 |
| N-01 | P1 | 表达式提交失败丢失动画且无撤销 | A | 1–2日，立即处理 |
| N-02 | P1 | 同节点表达式依赖编辑顺序 | A | 2–3日，与N-03同批 |
| N-03 | P1 | UUID别名/本地循环绕过提交校验 | A | 1–2日，与N-02共用resolver |
| N-04 | P1 | loop/valueAtTime设置时删除采样轨道 | A | 3–5日，依赖N-01–03统一政策 |
| N-05 | P1 | 动画子Transform丢失父变换 | A | 1–2日，唯一参数解析层 |
| N-06 | P1 | 时间节点未冻结动画/序列/时间线源 | A+B | 5–8日，依赖N-05及统一求值帧 |
| N-07 | P1 | 共享源的不同时间分支读同帧 | A | 短期诊断1日，正确求值3–5日依赖N-06 |
| N-08 | P2 | 连续Retime折叠产生整数错帧 | A | 0.5–1日 |
| N-09 | P1 | Cryptomatte浮点epsilon导致误选 | A+B | 2–3日，与N-10同批 |
| N-10 | P1 | 合法Cryptomatte对象hash产生NaN | A | 1–2日，需持久化迁移 |
| N-11 | P2 | Cryptomatte合法JSON错误结构崩溃 | A | 0.5–1日 |
| N-12 | P2 | 表达式负数真值和短路错误 | A | 0.5–1日 |
| N-13 | P2 | 卸载插件仍遗留核心节点注册 | A | 2–3日，registry生命周期事务 |
| N-14 | P1优化 | 高关键帧量UI snapshot超过帧预算 | A并发初测 | 3–5日，最终独占重测；依赖N-05/06 |
| NG-01 | P2 | 4个AI节点仍为占位实现 | G | 按提供商与节点分别估算 |
| NG-02 | P2 | Retime默认光流选项没有实际插帧 | G | UI能力提示0.5日；后端3–5日依赖N-06 |
| NG-03 | P2 | 人脸检测及皮肤mask链未接线 | G+B | 5–8日，依赖真实模型可用 |
| NG-04 | P3 | 文档问号条件语法不可运行 | G | 修正示例0.5日或完整parser1–2日 |
| NG-05 | P2 | Volume仅CPU npz吸收MVP | G | UI提示0.5日；完整GPU/VDB先做设计 |
| NG-06 | P2 | Roto仅前16点提供动画通道 | G+B | 3–5日，stable point ID及schema迁移 |
| R-01 | P1 | Paint/AI CPU float32 写入默认 float16 纹理，功能失效 | A-GPU | 1–2 天 |
| R-02 | P1 | Paint 对 straight-alpha 输入把 alpha 也预乘，alpha 被平方 | A-GPU（隔离 f4） | 0.5 天 |
| R-03 | P1 | 合成视频 limited 范围重复压缩 | A-CPU | 1–2 天 |
| R-04 | P1 | BT.2020 编码辅助函数把 BGR 当 RGB | A-CPU | 0.5–1 天 |
| R-05 | P1 | P3/BT.2020 图片序列转换对 BGR 用 RGB 原色矩阵 | A-CPU | 0.5–1 天 |
| R-06 | P1 | 广色域 SDR / ProRes 10-bit 提交前已量化成 8-bit | A-注入 | 1 天 |
| R-07 | P2 | 自定义 HDR 描述符被固定 HDR10/HLG 标签覆盖 | B-静态 | 1 天 |
| R-08 | P1 | 中途取消已写一帧就报告完成 | A-注入 | 1 天 |
| R-09 | P1 | 视频全部渲染失败仍导出全黑并报告成功 | A-注入 | 1–2 天 |
| R-10 | P1 | 图片序列渲染/写盘失败跳过却成功 | A-注入 | 1 天 |
| R-11 | P1 | 串行图片序列缺少 GPU / 解码资源 finally 清理 | A-注入+B | 0.5–1 天 |
| R-12 | P2 | 并行序列 context 工厂失败不会回退串行 | A-注入 | 0.5 天 |
| R-13 | P1 | 导出重建 eval context 丢失项目运动快门 | A-注入 | 1 天 |
| R-14 | P1 | 导出遗漏全局 3D Scene Settings | B-静态 | 2–3 天 |
| R-15 | P1 | 页面渲染输出读回固定丢弃 alpha | B-静态 | 2–3 天 |
| R-16 | P1 | 3D 材质不透明度 / alpha mask 无渲染作用 | B-静态 | 3–5 天 |
| R-17 | P1 | 有贴图时基色、金属、粗糙度 scalar 被覆盖 | B-静态 | 1–2 天 |
| R-18 | P1 | 材质贴图色彩空间丢失、线性输入二次 gamma | B-静态 | 2–3 天 |
| R-19 | P1 | metallic_in 与 roughness_in 争用同 ORM sampler | B-静态 | 1–2 天 |
| R-20 | P2 | 材质缓存未按 program 区分 uniform 状态 | B-静态，需模型复验 | 1 天 |
| R-21 | P2 | 体积文件改了仍复用旧 GPU 内容 | A-注入 | 0.5 天 |
| R-22 | P1 | 动态噪声/粒子/帧表达式被判为静态，只能导出单帧 | A-CPU+B入口 | 1–2 天 |
| R-23 | P1 | position/motion等数据AOV导出PNG/TIFF裁切有效数值 | A-CPU | 1–2 天 |
| RG-01 | 能力补齐/验证 | 合成Render Output无音频开关、source audio/mix接线；render_video没有add_audio_stream/write_audio_samples调用 | 静态能力盘点 | 按依赖设计后估算 |
| RG-02 | 能力补齐/验证 | 图片序列只有format，settings.options={}；16-bit PNG、EXR压缩/half/full、channels、alpha选项不可达 | 静态能力盘点 | 按依赖设计后估算 |
| RG-03 | 能力补齐/验证 | Volume3D为CPU固定前向投影的.npz吸收；.vdb显式依赖拒绝；pointcloud CPU2D正交点图，没有接Scene3D几何/相机 | 静态能力盘点 | 按依赖设计后估算 |
| RG-04 | 能力补齐/验证 | 材质高级字段transmission/clearcoat/sheen/subsurface/normal_strength/UVTransform有model序列化，但当前PBR无完整消费；面板玻璃preset只emit4个scalar | 静态能力盘点 | 按依赖设计后估算 |
| RG-05 | 能力补齐/验证 | pool有acquire_tiered_texture(export→f4)，大部分effect还走legacy.acquire_texture默认f2；linear_f32读回≠全链32bit计算 | 静态能力盘点 | 按依赖设计后估算 |
| RG-06 | 能力补齐/验证 | RenderExport blocking wait可到120s；shared context route仍同步等待，是否UI线程取决于controller入口 | 静态能力盘点 | 按依赖设计后估算 |
| RG-07 | 能力补齐/验证 | 预检对cycle/output/missing media有防线，但codec/transfer/depth/alpha/音频与模型能力缺少同一可视报告 | 静态能力盘点 | 按依赖设计后估算 |
| RG-08 | 能力补齐/验证 | Cryptomatte目前只构建manifest，metadata.status=manifest_only；object_id仍byte编码pick ID，未实现标准多层hash/coverage EXR writer | 静态能力盘点 | 按依赖设计后估算 |
| R-24 | P1 | 4K关窗后合成渲染线程仍未停止 | A-APP+B，10/16再次观察 | 定位1日、修复2–4日；依赖R-11/V-06 |

## 补充优化与验证计划

以下条目区分测试基础设施问题、测得的性能不足、明确未提供的能力、尚需实机验证的任务。未验证项不计入“已复现业务 bug”。工时为人日粗估，必须在定位后重估；共用基础设施的条目不能把工时简单相加。

| 编号 | 类别 / 优先级 | 问题与证据 | 解决方法 | 验收标准 | 粗估 / 依赖 |
|---|---|---|---|---|---|
| QA-01 | 测试基础设施 / P2 | 隔离运行 test_ai_remove_wiring 18例有4例因 OpenEXR disabled 失败；开启 OPENCV_IO_ENABLE_OPENEXR=1 后18例全过。src/main.py:51、shared/io/file_io.py:11已设置启动开关，因此这些不是4个生产效果bug。scripts/run_composite_tests.sh缺同等环境契约。 | 在测试/bootstrap最早位置、导入cv2前设置并验证OpenEXR能力；测试runner与产品启动共用受控环境前提；缺codec时提供可定位错误，不能依靠测试顺序先导入FileIO才成功。保留本次失败与补环境复测证据。 | 单文件/整套/隔离runner使用相同启动条件；AI cache真正写读EXR成功；无EXR构建给明确能力诊断。 | 0.5日，独立 |
| QA-02 | 测试崩溃 / P1 | test_fix_nodes1_aud2.py:194声称fake GL且无需context，实际FileOutputNode._query_draw_fbo_binding在file_output_node.py:264调用原生glGetIntegerv，进程SIGSEGV(-11)。该文件未完整执行，不能算全套通过，也不能据此声称真实用户窗口必崩。 | fake单测同时mock FBO查询/恢复等全部原生边界；另在真实GPU测试里检验真实FBO恢复；测试标记真实GL需求，runner继续进程隔离并保留returncode、stderr、未执行用例。不得用宽泛except宣称可捕获native crash。 | 原文件完整执行无native调用；真实context下readback和故障清理通过；整套无SIGSEGV；无可用GPU时明确skip而非假通过。 | 0.5–1日；可立即修测试，但不能代替R-01真实pool回归 |
| QA-03 | 基准可复现性 / P2 | tools/bench_composite_playback.py:24硬编码topos-color01并插到sys.path首位，本次在topos-color02直接执行可能测错checkout。bench_all_composite_nodes_real_topos.py固定历史输出目录/已退役素材名；GPU coverage script视频只找Windows桌面目录。 | 全部以__file__定位本checkout；媒体用tests/real_media.py和冻结清单；提供media/output路径参数；JSON写HEAD、dirty状态、源路径/指纹、设备/驱动、质量/代理/输出尺寸。只复用内容签名一致的提取帧。 | 从含空格目录、另一个checkout、CI均测当前HEAD；无素材明确blocked；不覆盖旧日期结果；输入与报告媒体身份相同。 | 1–2日，独立 |
| QA-04 | 性能/正确性门禁不足 / P1 | GPU coverage的静态image_input跨frame命中缓存，不能代表视频每帧效果成本；LUT override使用file_path而实际参数为lut_path，Defocus覆盖size而实际为max_radius，可非空直通却被判通过；all-nodes driver仅因“状态可解释”返回0，failed也可退出0。现有大量测试对mock/结构/同样错误的串并行一致性断言，未覆盖本次多个输出错误。 | 校验参数键/有效port及目标节点确实被求值；动态视频/参数扰动与cache-hit分别测；非identity fixture与直通参考做像素差和目标数学断言；模型/后端资源blocked与功能failed分开；真实app benchmark原counted_submit把ScopeDemand当dict导致AttributeError并abort，计数器必须只处理dict request；本次已在审计副本修复并复测、逐场景重置配置；原脚本不调用mw.close而直接SystemExit，live CompositeRenderThread在interpreter终结时SIGSEGV，不能当正常用户关窗缺陷；副本已走生产closeEvent（临时工程的question明确返回Discard）并检查线程终止；失败分支有异常退出清理。产品源码未改。CLI提供inventory模式与strict gate模式，strict出现failed应非0退出。将本次各定向复现转成行为回归，用真实pool和真实encode→decode补关键路径。 | 错lut路径、无效参数、Paint直通、全黑成果、缺帧成果故意注入时门禁必失败；静态cache数字不混作视频效果FPS；报告区分failed/blocked/skipped，所有CI错误可定位。 | 2–3日；随U/N/R修复分批纳入 |
| C-01 | CLI能力缺口 / P2 | scripts/topos_render.py:204仅接受exr/png/jpg/jpeg，没有视频编码入口；交互导出的视频能力不能通过无头CLI使用。 | 复用CompositeRenderOutput的视频路径与同一preflight/输出schema，增加codec/pix_fmt/descriptor/fps/帧范围/取消参数和结构化结果；不另造求值器。先完成R-03～10的色彩与完整性合同，再开放。 | 相同graph/frame范围GUI与CLI像素/帧数/色彩标签一致；错误退出非0，取消明确；序列原有用法兼容。 | 2–3日；依赖R-03～10 |
| P-01 | 已测性能不足 / P1优化 | 真实2K/4K TOPOS、DNxHR及ProRes4444的简单视频源→Output离线预览明显慢于H.264/H.265/422来源；详细数值见性能表，部分4K远超25fps的40ms预算，4K TOPOS422 Proxy中位733.8ms、复杂图726.9ms；名字带Proxy不代表合成节点当前自动使用低分辨率。frame trace仅能先定位到eval段，不能凭此认定全部是GPU算力不足。 | 将eval分成seek/decode、planar→RGBA、IDT、CPU拷贝、upload、节点GPU、readback；记录每帧后端及降级原因。按测量优先复用native/GPU planar输入、减少uint16→float32→拷贝转换；preview使用可重建代理与GPU IDT，export保持高位深合同。按codec/profile建立能力矩阵，避免所有高位深来源统一走昂贵CPU路径。 | 同批媒体同尺寸同质量独占复测；低成本图preview p95目标≤40ms(25fps)、专业目标≤16.7ms(60fps)，未达到时提供有效代理档并证明无错帧；最终导出不通过降位深换速度。 | 分解1–2日，优化3–5日；依赖QA-03/04及R-06 |
| P-02 | 已测跳帧不足 / P1优化 | 4K随机100→240→10→400来源帧的同步离线请求存在数百ms至秒级尾延迟；4个seek只是探查，不能当成熟p95 SLA或认定GUI一定同样卡住。 | 对GOP索引、解码重排/关键帧距离、shared provider cache、source→comp帧映射做分段计时；scrub合并latest请求并丢弃过期结果；关键帧附近小范围预取，代理缓存按文件签名/帧/色彩/质量区分。将UI seek-to-present端到端测量与同步渲染请求单独统计。 | 冷/热、正/反向、首/尾、长GOP至少100次seek；无旧帧回显；preview代理seek p95≤100ms，专业≤50ms；最终full-res精确请求不能以缓存错帧满足指标。 | 1–2日定位，2–4日优化；依赖N-06/07确保时间语义 |
| P-03 | 已测导出吞吐不足 / P2优化 | 多种4K简单图linear_f32离线导出每帧明显慢于srgb_u8预览，常见约0.5秒及更高；这是渲染+读回、未含实际视频编码，不能把数字称为导出成片FPS。 | 对full-frame/tiled strategy做相同精度对照，分解tile上游重复求值、texture pool、tile stitch、readback/PBO、CPU数组复制；在VRAM预算允许时比较full-frame f32+异步PBO与逐tile回读；源与公共上游按内容/源帧共享，逐tile工作只覆盖有效ROI。修R-11资源生命周期后再调并行度，保证owner-thread清理。 | 相同float输出与单帧参考误差达标、tile边界无缝；吞吐目标先提升≥2倍并写实测，p95/内存均不回退；1/2/4并行实际编码基准输出严格等价、无缺帧。 | 1–2日定位，3–5日优化；依赖R-11/15、N-06/07 |
| P-04 | 性能诊断不足 / P2 | 本次真实frame trace多项gpu_time_ms、cpu_upload_bytes、cache_hit、pool_peak_bytes为null。eval_ms/readback_ms虽可用，但不足以区分GPU同步、CPU解码和内存开销。 | 在已有PerfMonitor/frame trace补可用GPU timer query、每阶段CPU计时、上传/下载字节、真实cache命中、pool高水位、decode backend/fallback；能力不可用保留明确原因。采样/延迟回收查询，不能为监控每帧glFinish阻塞。 | 图、帧、request、source/quality有统一关联；同一问题可从报告定位到阶段；render-thread自驱播放的1次submit可以产生多帧，不能将emissions/submit当成功率；开启采样开销≤2%，不支持的GPU计时有原因。 | 1–2日；前置于P-01/02/03深入优化 |
| V-01 | 实机验证任务 / P1 | 核心编辑与保存完整旅程覆盖不足，定向slot测试没有证明整个真实窗口操作与最终磁盘文件全部正确。 | 建立新建CompClip→添加视频/图层→关键帧/表达式→撤销/重做→切Edit→保存.topos→关闭重开→Deliver旅程。复制工程到临时目录，比较source身份/层/图/视图/未知字段。含旧schema、非法表达式、失败载入和取消。 | 对U-01/02/05/11/12和N-01缺陷修复后至少5条真实旅程通过；最终落盘重开一致，原用户工程不改。 | 1–2日；依赖第一批持久化修复 |
| V-02 | 实机交互任务 / P2 | 关键帧、图层、曲线两种编辑方式以及焦点/快捷键的一致性仍需逐手势验收。 | 测鼠标选择/框选/多选、向量分量、Auto-key、Bezier切线拖动、expression错误、trim/slide、I/O播放区间、锁/solo、Ctrl+Z不同防抖时序、编辑↔合成transport联动。每一步比对live model/undo/预览/序列化。 | 形成可复跑手势清单与屏幕录像/状态快照；U-03/04/07/08/14/15/16不再复现；焦点在属性/搜索/曲线时快捷键行为清楚。 | 2–3日，依赖参数/连线事务修复 |
| V-03 | 大素材操作验证 / P2 | dependency scan/collect/relink存在同步service入口，本次未复制GB媒体，未证实具体UI阻塞时长。 | 在测试工程内对35素材做缺失/重复路径、重链、收集、取消、磁盘不足；测event-loop heartbeat，不把文件拷贝放UI线程；采用现有service的任务/进度接口、原子提交与取消清理。 | 重链后节点/项目/source provider一致且undo有效；收集保存可在另路径重开；UI heartbeat p95<5ms、无>20ms长阻塞或提供实测优化排期；不覆盖原素材。 | 1–2日验证，修复按测量另估 |
| V-04 | 大图恢复验证 / P2 | page loader逐节点重建视觉，尚无50/200/1000节点真实页面载入指标。 | 修U-06时复用既有batch build_from_graph；测解析、模型构建、连线/装饰、缩略图、layout/repaint分别耗时；缓存只保留可重建数据；加载有进度且可取消。 | 50/200/1000节点含组/便签/层分别p50/p95与UI heartbeat记录；模型/视觉数量准确，恢复无重复、无隐藏新图；目标指标以基准后设定。 | 1日验证，1–3日优化；依赖U-02/05/06/13 |
| V-05 | 显示与平台验收 / P2 | 本机没有验证真实HDR显示器输出、全部DPI/Dock尺寸、Windows/Linuxcompute和所有3D材质的视觉参考。 | 按原有view/renderer入口测alpha棋盘、HDR>1、通道/AOV、ROI/代理、A/B差值、像素检查、示波器、3D相机/透明材质/输入override；跨平台做GL4.1/compute/Metal支持矩阵，包含中英文和125%/150%/200%DPI。 | 每项记录设备与参考截图/像素差；可用能力有一致结果，不可用能力明确诊断；不把MacGL4.1性能推广到compute平台。 | 2–4日；依赖R-13～20与N-09/10 |
| V-06 | 长时稳定性验证 / P1 | 本次媒体基准是短样本、独立进程，未证明30分钟播放、反复导出/取消、窗口关闭和GPU/native frame lease的长期稳定性。 | 在独立测试实例跑30分钟连续+循环播放，100次seek/切页、20次串并行序列导出与取消、关闭窗口；记录RSS/VRAM/pool/decoder session/线程数与分位延迟。区分正常bounded cache和不可回落增长。 | 无崩溃/死锁/过期帧；取消和关闭在owner-thread清理；预热后资源在预算内稳定、停止后可回落；任何native crash单独保存栈。 | 1–2日；依赖R-08～12生命周期修复 |

| V-07 | 已观察冷启动失败 / P1验证 | 连续真实实例中，前例关闭保存current_page=Composite；下例冷启动直接进入Composite，30秒后renderer仍未初始化，日志等待gl_initialized。这是初始化失败观察，不是已经播放4K后崩溃，随后SIGSEGV来自异常退出未清理。 | 每例基准重置配置，分别以Color/Composite为起始页验证；沿页面on_enter、共享GL创建、gl_initialized绑定检查是否互相等待；必要时由公共GPU初始化service在任意GPU页面首次进入时触发，避免靠先打开Color。异常退出也走生产closeEvent。 | 从全新/已保存Composite偏好启动，3秒内完成共享GL与renderer初始化或给明确错误；无需手动先切Color；实例关闭后线程归零；不把测试生命周期错误算产品渲染崩溃。 | 0.5–1日定位，1–2日修复；依赖V-01/05/06 |

## 实施批次与依赖

| 批次 | 要解决的核心风险 | 范围 | 工作顺序与完成门槛 |
|---|---|---|---|
| B0：保存与状态安全 | 工程信息丢失、跨合成改写、失败操作丢动画 | U-01/02/03/04/05/09/11/12/13，N-01，QA-01/02 | 先统一graph payload与session事务，再修pending与失败回滚；真实临时.topos保存重开旅程V-01通过。可先单独修测试runner，不能当业务已修。 |
| B1：图编辑与动画语义 | 错误撤销、幽灵线、局部表达式与父子变换错误 | U-06/07/08/10/14/15/16，N-02/03/04/05/12 | 连线替换/粘贴同一个命令事务；参数求值唯一入口；loop与base轨道政策带迁移；model/visual/undo/serialization一致。 |
| B2：可靠渲染与交付 | Paint无效、颜色/位深/透明错误、取消/丢帧假成功 | R-01～13、R-15、R-22/23/24，RG-02/05/07，QA-04 | 先修真实pool上传与色彩合同，再修Result完整性/清理；encode→decode色条、透明图、缺帧/取消注入全部通过；禁止补帧默默成功。 |
| B3：时间与数据通道 | 共用源不同时间分支错帧、ID误选、AOV语义丢失 | N-06/07/08/09/10/11，RG-08，R-23 | 先统一(node,frame,port)请求与缓存，再补光流；Cryptomatte按位ID、标准输出与schema迁移；逐帧与外部EXR读取验证。 |
| B4：3D与能力补齐 | 3D预览导出不一致、材质控件无效、未接线功能 | R-14/16～21，NG-01～06，RG-01/03/04/06，C-01，N-13 | 先修可证实的设置/材质路径；R-20先复现混合shader模型；能力不足按真实可用资源分批上线；RG-03的volume与NG-05合并设计，不重复计算工时。 |
| B5：性能与稳定性 | 高位深预览、长GOP跳帧、4K导出、关键帧快照 | N-14、P-01～05、QA-03/04、V-02～07 | 先补阶段归因，再优化；错误输出修复后重建性能基线；快照及代理达到帧预算，30分钟稳定性和平台矩阵通过。 |

同一工程师的总体工期不能把每行相加；共享事务、色彩和求值基础设施可合并实现，也有迁移与平台复验成本。第一批实施应先完成B0的最小可验证修改，再逐批review和测试，不一次重写整个系统。

## 修复完成标准

每项都以“复现不再成立 + 用户预期结果正确”验收，不能只改索引状态为通过。状态建议使用：待修复 → 实现中 → 定向回归通过 → 集成/真实素材通过 → 完成。静态证据项先复现再改；模型/依赖阻塞独立列出，不能用skip计为功能通过。

工程/动画数据变更走既有command/service，保留undo、revision与未知字段；schema调整写迁移；GPU对象在创建线程清理；HDR与alpha由明确合同处理，不能通过clamp或降低位深掩盖错误。修复后重跑关联测试即可，最终再做一次隔离全套与同素材基准。

## 2026-10-16续验与新增计划

续验基线为`cdce6662cb7ab4468e84aeb2039792e38f1e9131`。原审计方案与证据已由`9e73315aa`归档。本次保留2026-10-14原始计数和性能数据，不把它们冒充今天全量重新测得的数据。

与原基线比较，`src/features/composite/`、`src/app/controllers/composite_controller.py`、`src/shared/project/save_router.py`、核心shared色彩/媒体/GPU/导出路径没有差异。MainWindow的变化是新增Edit批量参数信号连接，shared/keyframe新增轨道平移/缩放方法，没有修改原有合成求值实现。因此没有为了续写报告重跑整套7711个用例；改为重跑3个UI复现脚本和1个节点复现脚本。四个脚本均正常结束，保存字段丢失、跨图撤销、连线幽灵状态、表达式/时间语义错误仍可观察。今天记录见`docs/audit/2026-10-16-composite-followup/evidence/recheck_results.json`及逐脚本日志；结束码0表示复现脚本运行完成，不表示产品问题已修复。

原始GUI驱动曾因ScopeDemand计数器和直接SystemExit发生异常，已按QA-04作为基准问题处理。后续生产关闭入口验证与这些早期异常分开：2K HEVC关闭成功；4K HEVC在2026-10-14实际播放后窗口关闭但线程仍活着，驱动返回2；2026-10-16使用每个场景独立临时目录再测，4K HEVC仍出现相同状态。故补充R-24；不能再笼统把所有退出异常都归到测试脚本，也不能把未停止线程直接描述为“已证实用户一定崩溃”。

### R-24：4K播放后关闭窗口，渲染线程未退出却继续完成关窗

| 字段 | 详细计划 |
|---|---|
| 优先级 / 状态 | P1 / 已运行观察，待修复。证据A-APP+B源码。与早期未走closeEvent的测试脚本native崩溃区别记录。 |
| 复现环境 | 本机Qt6.10.0 / OpenGL4.1；独立临时config；导入`4K_H265.mov`，建立VideoFrame+Constant→Merge→Output，1920×1080合成、24fps，播放15秒。保存确认仅对临时合成明确返回Discard；真实`MainWindow.closeEvent`与既有cleanup完整执行。 |
| 实际结果 | 原始窗口测量224次frame_ready、14.9/s；今天复测210次、14.0/s。生产关闭后日志两次提示`Composite render thread did not stop within 3000 ms`，最终`composite_renderer_running=True window_visible=False`，驱动返回2。不是成功退出，也不是尚未接受保存确认。 |
| 影响 | 窗口树和共享GL宿主可能先于渲染线程销毁。仅保留QThread的Python引用不能证明context/surface与native frame lease仍有效；之后的异常释放或进程残留需要单独测，不能凭本次状态推断具体native崩溃根因。 |
| 定位 | `src/features/composite/render/composite_renderer.py:356` stop_thread返回停止结果；`src/features/composite/ui/composite_page.py:1951`超时仅保留引用和记录日志，cleanup没有向上返回失败；`src/app/main_window.py:2094`在所有owner停机完成前设置`_pipeline_shutdown_done=True`，closeEvent继续销毁窗口。原审计与当前版本该关闭逻辑相同。 |
| 已知根因与待定位点 | 关闭结果合同丢失已经有源码与运行证据。线程具体阻塞在解码join、GPU等待、scope还是pool/node释放，尚无足够现场栈，不能猜成某个GPU驱动bug。现有日志的重复3000ms等待不是可靠的资源完成确认。 |
| 修复步骤1：定位 | 在既有stop/cleanup各阶段加时间戳与owner线程ID；超时只抓取线程栈和未完成GPU lease/fence，不跨线程释放。先复现空窗口、2K、4K原片、代理片、播放中关窗和暂停后关窗，区分输入依赖。 |
| 修复步骤2：传播结果 | page cleanup返回明确stopped/timeout/failed及具体owner；MainWindow汇总结果，只有所有必须终止的线程完成后才设置stopped并允许最终销毁。不能先把整个管线永久标done而丢掉重试机会。 |
| 修复步骤3：有序停止 | 先停止timer/新提交与后台预取，发取消并完成/放弃在途请求；在owner线程回收pending decode/native frame、等待消费者lease/fence，释放node/cache/pool/output/scope/PBO，最后doneCurrent并销毁共享context/surface。所有入口幂等。 |
| 修复步骤4：超时处理 | 使用异步关闭状态机保持UI可响应及GL宿主存续；超时保留有效owner并明确停机未完成，可继续等待或重试。不得将强制terminate线程、吞异常、无界UI wait或单纯延长固定超时作为完整修复。 |
| 验收 | 2K/4K各至少5次真实关窗，标准进程退出0；窗口最终隐藏/销毁前renderer、decoder worker均停止且cleanup完成。注入超时后不销毁仍被使用的context，也不假报停止；取消关闭后原页面仍可用；重复cleanup不double-free；再执行V-06长测。 |
| 工时 / 依赖 | 1日抓取并定位阶段；2–4日修复与focused回归，依赖R-11资源生命周期及V-06。若涉及驱动原生等待，按现场栈另估。 |
| 证据 | `docs/audit/2026-10-14-composite/evidence/4K_H265.mov.real_app.log`与`docs/audit/2026-10-16-composite-followup/evidence/4K_H265.mov.real_app.log`；runner、逐案例临时config和结束码见两次gui结果JSON。 |

### P-05：真实窗口4K HEVC持续播放中后段退速

| 字段 | 详细计划 |
|---|---|
| 优先级 / 证据 | P1性能 / 两次真实frame_ready信号与播放头记录，短时15秒。不是物理显示器呈现测量，也不能用离线18ms结果宣称这个播放路径实时。 |
| 复现与症状 | 同R-24的简单合成。原始播放平均14.9/s，后半段每秒约7～8次回调；今天平均14.0/s，前段约23～24/s，7秒以后持续约7～8/s，播放头仍按24fps推进。只看“无超过1秒stall”会漏掉持续掉帧。 |
| 已有归因 | 今天drive日志每帧eval约3.4ms、render约1.7ms、等待约42.8ms、scope约5.1ms、retry共2458次。等待占比高，但没有完整decode/backend/cache事件链，尚不能把全部退速归于GPU效果、TOPOS代理或硬解。 |
| 解决方法 | 将请求、有效源帧、实际呈现帧号、VT/native ready、decoder切换、代理生成/启用、GPU lease/fence与scope需求时间串联；按秒记录distinct帧而非仅signal总数。做自动代理开/关、固定原片/已知代理、scope开/关、单一decoder后端对照，定位退速发生的首个事件。随后改有界等待/唤醒、取消过期请求、释放旧lease、控制代理切换，避免重试空转。沿已有VideoContext/service/渲染队列实现，不另起一条播放管线。 |
| 验收标准 | 相同素材及合成至少60秒并循环测试，实际不同帧按目标24fps稳定，持续一秒窗口不再降至7～8fps；呈现帧龄、连续丢帧、UI heartbeat和预览质量同时记录。平均FPS、无长stall和总signal数不能代替此验收；不靠丢画质或重复发同帧信号过关。 |
| 工时 / 依赖 | 1–2日事件链定位，2–4日优化；与P-02/P-04共享监测，依赖N-06/07保证源帧语义，R-24保证测试可靠结束。 |

### 续验结论的适用范围

今天TOPOS 4K来源的1080p合成短测为359次frame_ready、23.9/s，无超过1秒信号间隙，正常关闭后renderer与窗口均停止，进程退出0。这是本次受测路径的正面结果；不能把原先离线4K读回慢推广为TOPOS全部交互播放均不实时。HEVC的退速和关闭超时按R-24/P-05继续排期。原始基准未完成的最后一组窗口测试至此已有结果；V系列的长时、全交互与跨平台验收仍是后续修复验收任务。

### V-07补充证据：直接冷启动Composite时GL初始化等待

早期日志`4K_H265.mov.real_app.fifth.log`显示启动页为Composite，30秒内未建立renderer，停在等待`gl_initialized`；异常退出本身又触发测试生命周期问题。当前源码`composite_page.py:1226`只向父链查找`canvas.gl_viewport`，`:1293`成功连接它的就绪信号后等待，`:1547`的兜底重试不主动初始化一个尚未显示的donor viewport。这能解释“先打开Color可用，直接进入Composite可能等待”的风险，但仍需以正常应用入口排除harness启动时序。本项继续按V-07待验证任务记录，不计入确认bug数。

复验应分别从Color、保存过的Composite及独立Composite产品入口冷启动；要求共享Qt GL宿主在任何GPU页面首次进入时可完成初始化，不依赖用户先打开Color。若采用公共初始化服务，保持GL/ModernGL求值在既有渲染线程，不能由UI临时直接跑GPU操作。记录ready信号、context有效性与首帧时间，避免把“信号已连接”当“初始化已触发”。


## UI、图编辑、撤销与工程持久化详细计划

### 问题索引

| ID | 优先级 | 问题 | 证据级别 | 预计工时 / 依赖 |
|---|---|---|---|---|
| U-01 | P1 | 图层/图层栈没有同步回 EditProject | 生产方法独立复现 | 4–8 h；依赖保存契约 U-11 |
| U-02 | P1 | 切合成后共享旧撤销历史 | 真实 page/stack 独立复现 | 8–16 h；与 U-05/09/13 共用 session reset |
| U-03 | P1 | 批量参数防抖混用原值，undo 写坏另一参数 | 真实 page/stack 独立复现 | 4–8 h；与 U-04 共用事务边界 |
| U-04 | P2 | 待提交参数还未入历史时 Ctrl+Z 撤错步骤 | 生产方法即时顺序独立复现，实机时序未测 | 4–8 h；依赖 U-03，计入统一事务修复 |
| U-05 | P1 | 空图载入保留上一合成节点和格式 | 真实 loader 独立复现 | 4–8 h；与 U-02 共用 loader/session reset |
| U-06 | P2 | 保存的 Backdrop/便签载入后没有视觉对象 | 真实 loader 独立复现 | 4–8 h；与 U-05/09/13 共用恢复入口 |
| U-07 | P2 | 节点旁路/启用禁用不能撤销 | 真实 action/stack 独立复现 | 4–8 h；独立命令，复用 UndoStack |
| U-08 | P2 | Project Settings 修改不能撤销 | 真实 slot/stack 独立复现 | 8–16 h；依赖格式元数据提交契约 |
| U-09 | P2 | 新合成继承上一合成节点锁 | 真实 editor/loader 独立复现 | 2–4 h；可并入 U-02/06，勿重复计时 |
| U-10 | P2 | 选择上游/下游只遍历一跳 | 4 节点真实图独立复现 | 1–3 h；独立，可先修 |
| U-11 | P1 | 合成页保存覆盖来源/身份/扩展元信息 | 真实 SaveRouter，捕获落盘前对象 | 8–16 h；第一优先，包含持久化集成回归 |
| U-12 | P1 | 保存查看器状态覆盖节点编辑器/输出目标状态 | 同上 | 4–8 h；依赖 U-11，分 schema 迁移另估 |
| U-13 | P1 | 旧分组视图过滤让新合成节点全部隐藏 | 真实 group view/loader 独立复现 | 2–4 h；可并入 U-02/06，勿重复计时 |
| U-14 | P1 | 替换单输入连线后 undo 不恢复旧线 | 真实 action/graph/stack 独立复现 | 8–16 h；连线事务基础，先于 U-15/16 |
| U-15 | P1 | 多输入断线误删其他视觉线，产生幽灵线 | 真实 Scene3D 多输入独立复现 | 8–16 h；依赖 U-14，包含多输入精确删除 |
| U-16 | P2 | 粘贴已有连接图生成多余的 Connect 历史 | 真实 clipboard/action/stack 独立复现 | 4–8 h；依赖连线事务 U-14 |

工时为单人实现+focused回归+review的粗估；多个项目共享修复时应合并排期，不能把表中数字机械相加。全部属于已证实生产处理路径问题；实机鼠标/窗口、真实落盘及GPU性能限制见下文。

### 审查范围、证据与限制

项目目录：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02`。已阅读 `.clinerules`，本次仅审查和制作复现证据，未修改产品代码。主要覆盖 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/app/controllers/composite_controller.py`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/runtime.py`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/project/save_router.py`。

以下 16 项全部有独立 Python 复现；其中 U-15 包含两个变体。复现使用项目 `.venv/bin/python`、PyQt6 offscreen、真实 NodeGraph / CompositePage / NodeEditorWidget / UndoStack，屏蔽 GPU 求值、素材扫描和重型外部服务。保存问题使用真实 SaveRouter 的更新路径，最后落盘函数替换为返回成功，以避免生成/覆盖用户工程；已证实传给落盘层的对象错误，但未在这个子审查中操作用户真实 `.topos`。

本报告未进行操作系统原生窗口的逐按钮实机点击、真实显示器色彩观感、GPU/视频播放性能测试。某些重现直接调用与 UI 信号相同的 slot/action；本文把“该生产处理路径确定失败”和“用户实际鼠标/快捷键时序已实机验证”明确区分。根任务负责更大范围的自动化测试和测试视频性能基准。历史计划中声称的缺陷没有作为本次证据使用。

#### 复现与输出文件

| 文件 | 覆盖项 | 运行方式 |
|---|---|---|
| `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_ui_audit_repro_20261014.py` | U-01～05 | 项目根目录 `.venv/bin/python /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_ui_audit_repro_20261014.py` |
| `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_ui_audit_more_20261014.py` | U-06～13 | 同上，换脚本路径 |
| `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_ui_audit_connections_20261014.py` | U-14～16 | 同上，换脚本路径 |

复现脚本不会写用户媒体或工程；offscreen 下无需创建 OpenGL context。日志中的字体/locale 提示是环境提示，不能算产品崩溃或性能结论。

### 功能覆盖清单

| 用户功能域 | 已检查的入口/数据流 | 本次结论 |
|---|---|---|
| 进入、退出、重新打开合成 | page controller → composite controller → load/context/on_enter/on_leave | 查到空图载入污染、历史串图、锁/分组视图串图；真实切页手势仍需实机验收 |
| 节点新增/删除/移动 | actions → page slot → NodeGraph → undo/render | 阅读新增/删除及资源前置清理；本子任务不声称这些所有交互都已通过 |
| 节点属性、批量属性 | debounce、参数提交、批量修改、undo | 证实批量归属错误与 Ctrl+Z 未刷新 pending |
| 节点启停/旁路 | H shortcut、toolbar/property enabled → graph disabled | 状态能改，但没有对应 undo |
| 连线、替换、断开、反拖、多输入 | action/connection signal → page connect/disconnect → model/visual/undo | 证实旧线恢复丢失、多输入模型/视觉不一致 |
| 复制、剪切、粘贴、重复 | clipboard → paste/connect → grouped undo | 证实连线重复进入历史；普通剪切/重复的完整用户旅程仍需实机验收 |
| Backdrop、便签、分组 | model persistence → visual reconstruction、group filter | 证实装饰载入后不显示、旧组过滤隐藏新图 |
| AE 图层视图、图层顺序/可见性/父级 | LayerStackController → NodeGraph serialization → EditProject sync | 证实 live sync 字段白名单遗漏图层数据 |
| 关键帧、曲线、表达式、time range | panel signal → page slot → keyframe/expression model、undo | 阅读绑定与轨道恢复路径；根任务统一跑已有覆盖测试，不能把单元测试等同全部实机功能 |
| 播放头、播放范围、I/O、Edit transport | 独立 comp timer / transport delegate、双向 range sync | 阅读范围、播放/seek 接线；实际帧率/音画同步由根任务性能检查 |
| Project Settings | fps/duration/background/output/Scene3D/Constant 更新 | 证实整项设置没有 undo |
| 工程保存/加载、查看器状态 | runtime serialization → SaveRouter → EditProject/topos | 证实 composition identity/source/扩展元信息被覆盖、viewer 子状态被覆盖 |
| 素材依赖、重链、收集 | panel → AssetRelink/Collect service → undo/publish | 阅读服务入口及错误回报；本报告没有实际收集大文件或覆盖路径 |
| 3D 场景、材质、Outliner、动画信息 | selected Model3D → diagnostics/panels → params | 阅读选择与状态接线；只用 Scene3D 多输入复现连线问题，没有声称所有 3D 工具已验收 |
| 节点预览、Viewer、A/B、ROI、通道、HDR显示 | selected node/output target → RenderRequest；viewer persisted state | 阅读显示/请求路径，发现 UI 状态保存覆盖；像素和性能由其他审查负责 |
| 节点搜索、预设、导航、缩略图、minimap、Perf | registry/catalog/actions/navigation/thumbnail policy | 证实上下游导航只遍历一跳；其他入口作为后续实机 QA 清单 |

### 优先级与实施顺序

- **P1：优先修复**，会造成工程信息丢失、跨合成修改、图连接损坏、画布不可编辑、模型/视觉不一致。
- **P2：随后修复**，撤销行为/导航/恢复体验不完整，仍影响编辑可靠性。
- 排期建议先完成保存与会话边界（U-11/12/01/02/05/09/13），再修连线事务（U-14/15/16），再完成参数及设置撤销（U-03/04/07/08），最后补 UI 恢复/导航（U-06/10）。优先级是对风险的判断，不是承诺工期。


### 详细修复计划

#### U-01：图层与图层栈未同步回 EditProject

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/app/controllers/composite_controller.py:375`、`:393`；页面序列化和加载参见 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2244`、`:2327`。
- **触发/复现**：打开一个 Comp Clip，给页面图修改 `layers` 和 `layer_stacks`，调用 `_sync_composite_graph_to_edit_project`。仅这两项改变时，比较逻辑直接返回。复现输出 `layer sync result: None True`，即工程仍是原对象，根本没有获得新层数据。
- **用户影响**：图层顺序、可见性、solo/lock、父子级、名称等不能成为 EditProject 的可靠保存来源。含图连接变动的图层操作可能同步了节点/边，但仍漏 layer metadata，重开页面可退化为无层或旧层。显式合成 Ctrl+S 可能临时保存层，但遇到 U-11 另一类信息损失。
- **根因**：live sync 使用手写 graph_keys 白名单，没有 `layers` / `layer_stacks`，比较和 merge 都漏掉。实现层已经序列化这些字段，不是 LayerStack 不能保存。
- **解决步骤**：1）把 graph-owned payload 字段定义为可共享且有版本的契约，追加层字段；2）merge 中明确支持删除最后一个 layer/stack（不能以字段缺失表示删除，否则旧层永远留下；保存空 dict 或明确 tombstone）；3）让页面首次图层编辑和元信息编辑均通过统一持久化/revision提交入口；4）同步后使 Edit/Deliver frame-provider 指向新工程并失效 comp cache。
- **验收**：真实 LayerStackController 新增两层，排序、隐藏、设置父级、删除到 0 层，离开到 Edit 保存，重开后逐字段一致；只改层 metadata 必须使 dirty/revision变化；undo/redo 和最终落盘的 layer/node topology一致。

#### U-02：撤销历史跨合成使用

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2133`、`:1975`、`:829`、`:7518`。
- **触发/复现**：A 图中 node_id=`shared` 的 rotation从 0改为20并提交；load B，B 同 ID 的 rotation=70；Ctrl+Z 对应 `page.undo()`。实测 B被改成0。不同 node_id时，旧 add/remove/connect action也可能操作旧 ID、重加旧节点或成为无效记录。
- **根因**：改变 composite_id只重置 one-shot edit 标志；load、reset_to_empty_workspace、源图切换没有清空或切换 UndoStack，也没有隔离 pending param/batch/nav/interaction。旧 lambda从 `self._node_graph` 解析当前图，跨图使用。
- **解决步骤**：1）新增 composition session切换入口，先提交/取消本图的 pending transactions并停止相关 timer；2）最小修复是新图标识变化清空当前 stack，长期方案按 composite_id维护独立 undo上下文；3）load validation成功后才原子切换 session，失败保持原图/原history；4）scratch reset也清历史、pending、选择、预览、group/lock transient state；5）避免同一身份的重复 load把当前编辑历史无谓删除，定义 reload语义。
- **验收**：A/B含相同和不同 node IDs分别测试；B的Ctrl+Z不能修改A或恢复A节点；回A后按既定策略恢复A历史/清空，行为明确；80ms内切图不会把A pending参数写入B；load失败原图历史可用。

#### U-03：批量参数防抖写坏其他参数

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:7063`、`:7091`、`:7149`。单参数已有归属校验 `:6916`，批量分支未采用。
- **触发/复现**：两 Transform节点 rotation原0、scale原1，在一次80ms窗口先批量rotation→10，接着批量scale→2，flush后undo。实测两节点 rotation仍10且scale变0；预期两个可独立撤销步骤，scale第一次undo回1、第二次rotation回0。
- **根因**：已有 pending条目时总保留 orig_old，却把 node_ids/param_name替换为最新操作，导致旧参数的原值套到新参数。切换多选集合也会借用另一集合的 old_values。
- **解决步骤**：1）以 `(tuple(sorted(node_ids)), param_name, interaction/session id)`识别批量事务，归属变化先flush旧项；2）原值从live model写入前捕获、深拷贝并限定在该事务；3）batch/single pending互斥或有统一提交顺序；4）支持 `None`/不存在参数的精确恢复，不用 `old_val is not None`静默跳过；5）绑定真实drag begin/end减少单靠时间窗口合并。
- **验收**：上述rotation/scale复现，重叠/完全不同node集合，单/批切换，颜色tuple/普通float分别覆盖；一次完整拖动是一步，多次不同参数是多步，每个undo精确恢复参数及关键帧状态。

#### U-04：Ctrl+Z未先提交待防抖修改

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:7518`、`:7525`、`:6985`、`:7149`。
- **触发/复现**：先完成rotation修改，再修改scale但不等80ms，立即undo。实测undo撤掉rotation，scale仍保留，之后timer/手动flush又给scale创建一个新历史分支。实机快捷键时序未操作，但同一生产入口顺序已复现。
- **根因**：undo()/redo()直接操作stack，pending param/batch没有flush，timer可在撤销后继续提交新action。
- **解决步骤**：1）所有undo/redo入口（快捷键、toolbar、history tree、外部脚本）集中至page事务边界；2）在undo前停止debounce timer并按提交顺序flush param/batch/interaction/nav；3）若仍拖动，定义结束并提交或取消，不让拖动后续tick复活已撤销动作；4）redo前pending新的真实编辑必须明确使redo分支失效。
- **验收**：0/20/79/81ms延迟的快捷undo；当前参数应先恢复；flush后无意外历史条目；redo正确；跨图、关闭页面和拖动中Undo同样覆盖。

#### U-05：载入空图保留上一合成内容

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2132`、`:2146`。
- **触发/复现**：先load有节点、24fps、48帧的A，再load `{'composite_id':'empty','nodes':[],'fps':30,'duration_frames':2}`。实测节点仍为A及后加节点，fps仍24、duration仍48，但当前身份已是empty。
- **用户影响**：新/空/旧版本空合成显示上一合成内容，之后graph_changed/保存可能把旧节点写到新的composition ID。即使常规empty Comp Clip factory有默认Constant+Output，合法空图/旧工程/损坏数据仍会到这个入口。
- **根因**：先切身份再检查nodes，空nodes提前return，清图、元数据设置与visual重建都跳过。
- **解决步骤**：1）区分缺失nodes字段（格式无效）与nodes=[]（合法空图）；2）统一validate→construct→commit顺序，合法空图载入空模型或约定Output-only空工作区；3）fps/duration/view状态独立于节点数量恢复；4）失败不更换当前身份，显示诊断；5）last texture/preview清空，避免画面缓存残留。
- **验收**：有图→空图之后无旧节点/层/边，fps/duration为新值；空图save/reload不复活旧图；缺字段/非法结构保持原图；page ID、display、EditProject三方一致。

#### U-06：Backdrop/便签只载入模型，没有生成视觉对象

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2274`（backdrops/sticky_notes赋值）；正确重建入口 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:866`、`:880`。
- **触发/复现**：工程payload带1个Backdrop和1条便签，调用load。实测model计数1/1，editor `_backdrop_items`、`_sticky_note_items`均0。
- **根因**：page loader手工逐个add_node_visual，最后赋decorations，未调用已有build_from_graph或相应add_*_visual；存储层有数据但恢复UI漏步骤。
- **解决步骤**：1）先完整恢复graph元数据，再统一 `build_from_graph`，复用批量模式、group/locks/navigation恢复；2）不要同时手工重建nodes/connections再build导致重复；3）thumbnail callback、node state、selection再绑定放在统一恢复完成后；4）保留backdrop包含节点、note颜色/位置/字号和层级。
- **验收**：saved/backdrop/sticky model和visual数量/内容/位置一致；move/resize/edit/delete均有undo；多次load无重复对象；含未知节点或损坏装饰仍有可读诊断，不让整体工程无法打开。

#### U-07：旁路/启停没有撤销记录

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_actions.py:1068`、`:1094`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:3625`、`:6414`。
- **触发/复现**：清空undo，actions.toggle_bypass('t')，undo。实测disabled仍True、can_undo=False。属性section的enabled handler同样直接改状态，没有push action。
- **根因**：bypass只更新model/visual/evaluate，未采用page/command统一状态提交入口，批量H也未成组记录。
- **解决步骤**：1）统一SetNodeEnabled/Disabled提交命令，捕获每节点旧值；2）H、菜单、属性标题、脚本等调用同一命令；3）多选旁路一条group undo；4）恢复时同步header、properties、layer row、预览/cache；5）锁节点统一拒绝，不在action已写后slot再拦截。
- **验收**：单节点H、属性启用checkbox、多选混合disabled分别undo/redo；只影响选中且未锁节点；一键H是一条记录；保存重开状态一致。

#### U-08：Project Settings不支持撤销

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2824`。
- **触发/复现**：改Output到640×360、fps30、duration5秒→150帧，再undo。实测设置保持640/30/150且history为空。background、Scene3D/Constant分辨率跟随也是直接写。
- **根因**：一项设置同时写page metadata、NodeGraph metadata和若干node参数，没有before/after快照或command。
- **解决步骤**：1）提交前捕获fps/duration/output size、需要跟随的Scene3D/Constant、marked background color与resolution lock状态；2）形成单个ProjectSettings命令；3）拖size/duration按真实交互事务合并；4）undo/redo同步Settings面板、frame range、playhead clamp、output/cache；5）timeline-bound合成继续尊重Edit拥有的格式，拒绝被禁用控件的间接修改。
- **验收**：分辨率/fps/duration/background逐项及组合更改undo/redo准确；用户自建Constant颜色不受背景改色；0-duration及非整数fps边界；保存重开后与视图值一致；修改duration后的关键帧/工作区边界有明确定义。

#### U-09：节点锁跨合成残留

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:845`、`:498`；page loader `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2250`、`:2269`。
- **触发/复现**：A节点t上锁，load无viewport_state的B，B同ID节点t仍is_node_locked=True。B没有显式保存锁，预期应可编辑。
- **根因**：clear_all不清 `_locked_node_ids`，page loader又只在非空viewport_state时调用set_viewport_state。正确build_from_graph有按当前graph过滤/初始化锁的逻辑，但这里绕过了。
- **解决步骤**：1）切身份清transient editor锁/selection；2）恢复当前图时使用build_from_graph的规范化逻辑；3）viewport字段缺失/空值恢复默认，不保留上一图；4）将持久化locks归属于composition UI schema。
- **验收**：A/B相同ID、不同ID、B空state、B显式锁集合、非法ID分别测试；B锁只来自B payload；properties read-only与viewer handles一致；同图重建不会意外丢锁。

#### U-10：上下游选择只到直接相邻节点

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:3870`、`:3888`；shortcut注册 `:2167`、`:2168`；actions使用 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_actions.py:1378`、`:1400`。
- **触发/复现**：Constant a→Grade b→Blur c→Output d。调用_get_upstream_nodes(d)只有c；_get_downstream_nodes(a)只有b。UI Ctrl+Up/Ctrl+Down调用这些方法，所以“所有上/下游”的能力不完整。
- **根因**：父层在递归前把邻节点加入visited；递归入口发现它已visited立即return，无法继续遍历。
- **解决步骤**：1）删除递归前的预标记，由每层入口标记，或采用deque/set的迭代BFS/DFS；2）统一根节点是否包含、多个起点是否保留当前选择的用户语义；3）仅返回仍在graph中的节点，避免坏边虚假选择；4）深图采用迭代避免Python recursion limit。
- **验收**：4节点链得到3个节点；分叉/汇合/多输入得到完整闭包且无重复；多起点与孤点；伪循环防护；1000级链不递归溢出。

#### U-11：合成页保存覆盖composition的身份、来源和扩展字段

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/project/save_router.py:921`、`:928`；graph-only来源 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/runtime.py:214`；fallback `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/app/controllers/composite_controller.py:915`。
- **触发/复现**：项目已有comp composition，内含comp_clip_id、comp_asset_id、composite_id、source_kind、name、width/height，以及timeline_id/clip_id/source_clip_id与custom_extension；把真实NodeGraph.to_dict的payload送进 `_save_composite_to_edit_project`。捕获落盘函数参数后这些字段全部缺失。保存路径成功返回，更新后的工程已同步callback。
- **用户影响**：Ctrl+S可能失去对应Edit clip/timeline context，重开后进入默认/非timeline路径；名称/格式 fallback变化，未知扩展字段也丢。`comp_clips`容器本身虽然保留，不能代表composition内的来源信息被保留。现有 `test_comp_clip_save_integrity.py`测试保留的是外层comp_clips，并没有覆盖同一composition被graph-only数据替换的情形。
- **根因**：router把新graph整体赋值给compositions[id]，没有merge已有记录；live sync controller已经知道要merge，显式save未复用同一契约。
- **解决步骤**：1）按现有composition为base，覆盖graph-owned字段，保留source/identity/unknown extension；2）composite_id强制使用当前key；3）统一live sync与save所用merge service，避免两处白名单再漂移；4）旧缺信息工程以canonical CompClip/Asset补全，timeline来源不能猜测；5）出错在内存提交前/落盘原子替换前失败，避免“写失败但内存已被改坏”。
- **验收**：timeline-backed/empty/image comp各创建真实工程→合成修改→Ctrl+S→关闭→重开，来源、clip范围、名称、格式、ID一致；注入未知字段往返保留；多个compositions互不覆盖；save失败工程原对象完整、dirty保留。

#### U-12：Viewer保存覆盖节点画布和输出目标状态

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/project/save_router.py:922`、`:924`；Viewer state键集合 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_viewport.py:11055`；editor合并逻辑 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:5180`；output目标保存 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2344`。
- **触发/复现**：graph.viewer_state有selected_node_id、asset_search_roots、output_target_name、output_target_node_id；保存传入独立viewer state `{'zoom_level':1.5}`。最终composition.viewer_state仅这个zoom字段，其他全部丢。
- **根因**：graph UI与display UI共用viewer_state键，SaveRouter所谓“合并”实际是整字典替换。runtime graph获取也直接node_graph.to_dict，未走page.get_composite_data补充当前output target。
- **解决步骤**：1）短期将 `{**existing_graph_viewer_state, **display_viewer_state}`深拷贝合并并指定冲突键所有权；2）调用page的规范序列化入口来捕获output目标；3）长期拆成graph_editor_state / composite_viewer_state并增加schema迁移，读取旧混合字典；4）确保selection仅恢复存在的node，search roots遵守工程可移植路径规范。
- **验收**：设置选中Model3D、Matte/AOV输出目标、资产search roots、viewer exposure/ROI/channel/zoom各项，save/reopen逐项恢复；旧混合state迁移；无Viewer时仅保存graph state，不把其清空；unknown state字段保留。

#### U-13：上一合成的group过滤让新图隐藏

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:324`、`:509`、`:845`、`:3648`；page loader `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2178`。
- **触发/复现**：A进入包含b/c的组g，load C仅有node `new-only`。实测group_view_stack仍['g']，new-only visual.isVisible=False，新画布是空的且旧group breadcrumb仍可残留。
- **根因**：clear_all有意保留group filter用于同图undo重建，page却在跨图load同样调用；没有跨身份session reset；逐add视觉执行旧过滤。
- **解决步骤**：1）区别同图rebuild与跨图load：跨图重置group stack/filter/branch isolation/dependency highlights/preview；2）新图如果需恢复group view只从该图persisted state获得并验证group仍存在；3）清breadcrumb并在load结束后同步；4）与U-02共享session reset，避免分别补零散状态。
- **验收**：在A深层组中打开B全图正常可见、breadcrumb root；B也含同名g但不同成员只按B过滤；同图undo/redo保持已进入组；删除当前group/成员有明确返回root策略。

#### U-14：替换单输入连线后undo丢失旧连线

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2381`、`:7386`、`:7401`；底层替换 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:1251`。
- **触发/复现**：a.result→o.image已经连上；把b.result接入同一个o.image。图正确改成b→o；undo后edges=[]，不是原a→o。
- **根因**：NodeGraph.connect自动替换旧input边，page仅记录“添加新边”，没有捕获被替换的边；undo只disconnect新边。
- **解决步骤**：1）connect提交前捕获目标input旧边与visual数据；2）把replace/reconnect定义为一个原子命令，包含removed_edges和added_edges；3）验证新边合法/无环/数据类型后才移旧边，失败保留原状态；4）undo按准确quad恢复旧边，redo准确替换；5）单输入/多输入分别定义，不能多输入每次全清。
- **验收**：a→o替换b→o，undo恢复a→o、redo恢复b→o，model/visual各一条；非法类型/环/锁端点拒绝后原线保留；重复连接不生成历史；连续replace三次逐步还原。

#### U-15：多输入断线误删其他视觉连接

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2400`、`:7401`、`:7411`、`:7449`、`:7472`；视觉删除 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:741`；action `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_actions.py:641`。
- **触发/复现A**：Geo3D g1.display和g2.display同时连Scene3D s.in（max_connections=8）；删g2→s。model剩g1→s，visual却剩g2→s，出现幽灵线且正常线视觉丢失。
- **触发/复现B**：相同两条线，对s.in调用disconnect_by_dst（输入口反拖入口用它）。action先删第一条visual，然后page slot再次按dst删除下一条visual，model剩g2→s，visual为空。
- **根因**：page和undo统一使用“按目标端口删第一条”，不按src/dst完整quad；disconnect_by_dst还违反“page独占visual删除”的信号契约。redo connect/undo disconnect也无条件删目标端口一条，影响无辜multi-input线。
- **解决步骤**：1）新增/复用remove_connection_visual_exact(src_id,src_port,dst_id,dst_port)；2）所有disconnect和undo/redo使用quad；3）disconnect_by_dst只选择要断的具体连接并emit，让page执行唯一删除；4）反拖多输入在有多条线时明确选择哪条，避免静默断错；5）连续事务后model连接集合和visual集合做debug契约检查。
- **验收**：Scene3D 2～8条线，任意顺序断其中一条、undo、redo、反拖分别保持其他边；visual/source port计数精确；到8条满载拒绝第9条且无变化；幽灵线清理不能删除其他线。

#### U-16：粘贴图的内部连接提前/重复进入undo历史

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_actions.py:907`、`:912`、`:916`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2704`、`:2710`。
- **触发/复现**：复制一对已有连接的Constant+Output，paste后历史顶是Paste nodes。一次undo删除新节点，下一条history仍是“Connect result → image”，关联已删除的新ID，成为多余无效步骤。含N条边会留下N个类似步骤。
- **根因**：paste通过actions.connect时page逐边已push_connect_undo；nodes_pasted随后才begin_group，又读取内部connections把它们录一次。新节点和连线未在开始时形成单个事务。
- **解决步骤**：1）paste开始前开启page/command事务，nodes与internal edges统一提交；2）采用已存在create_3d_scene_with_children那类“静默创建+一次汇总”的契约，保证内部connect没有单独记录；3）汇总只记录一次before/after或一次node/edge列表；4）部分失败原子回滚且不留下history；5）load_preset检查相同connect→nodes_pasted顺序并共享修复。
- **验收**：带0/1/20条内部边的粘贴各一条history；一次undo全撤，一次redo全恢复，model/visual无重复边；graph unknown node/受保护节点策略明确；preset load同样一条事务；回滚不留下孤节点、幽灵线或无效undo。

### 仍需实机/集成验收的不足（不当成已测出的新bug）

1. **完整操作旅程验证不足**：现有许多测试侧重数据对象、单slot或mock。例如CompClip保存完整性测试只测外层对象保留，不能捕获U-11。建议新建5条保存/编辑/切页/undo/reopen旅程；步骤要走真实controller和SaveRouter，而不是直接提供已经完整的composition payload。
2. **会话状态边界需要统一**：U-02/05/09/13都来自页面、editor与graph分别保留状态。应小步建立session契约并补focused tests，不做未经测量的大规模重写。
3. **关键帧/图层两种编辑模式的一致性需要实机QA**：selection、row filters、lock、auto-key、expression diagnostics、vector component、Bezier拖动、帧范围、合成/来源时间映射、I/O播放范围及undo group逐项走鼠标/键盘；不以已有unit tests数目替代验收结论。
4. **素材操作的UI响应待测量**：素材依赖扫描/collect在page slot同步调用service，应测大工程/大文件下event-loop阻塞和取消可用性。当前报告未测拷贝GB文件，因此只列验证任务，不宣称具体卡顿时长。
5. **大型节点图恢复耗时待量化**：page loader逐节点add视觉，而build_from_graph已有batch模式；修U-06时复用batch可降低重复包含关系/preview visibility扫描。需分别测50/200/1000节点载入、graph serialization、selection/repaint，给p50/p95和UI heartbeat延迟；不要凭复杂度估计写成实测速度。
6. **本子审查未验证真实显示**：HDR/alpha/checkerboard/AOV/ROI/A-B对比、不同DPI与dock尺寸、输入焦点/快捷键冲突、国际化文本和实际GPU性能纳入根计划的后续人工/集成验收清单。

### 建议回归层次

- 首层：基于3个复现脚本转换为focused test，不重复实现函数本身，断言用户预期最终状态。
- 第二层：model连接/visual连接/undo history/serialization四者一致性检查，真实page+controller+router integration。
- 第三层：Edit→Composite→Edit→save→restart的真实工程旅程，含失败/取消/旧schema/未知字段。
- 第四层：用户提供素材的GPU/视频性能与像素一致性；与本报告headless UI逻辑测试结果分开记录。

所有修复都应按 `.clinerules`：graph/project变更走已有model+command/service，GPU仅通过渲染线程任务队列，schema结构调整带迁移，未知字段保留。完成一项后复跑对应复现与关联测试，再做独立review。

## 节点、动画、表达式与时间求值详细计划

审查方式：只读源代码审查 + 当前虚拟环境运行真实 NodeGraph / BaseEvaluatorMixin 入口的定向复现 + 合成快照性能基准。未修改产品代码。未将历史计划中的描述当成当前问题。以下 A 级为实际运行复现；B 级为直接代码链证据但尚未做 GPU 像素/整页交互复现；G 为明确的能力缺口。

精确复现脚本：/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_nodes_repro_20261014.py；原始 JSON：/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_nodes_repro_20261014.json。性能脚本：/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_snapshot_perf_20261014.py。运行：在项目根目录执行 `PYTHONPATH=. .venv/bin/python /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_nodes_repro_20261014.py`。脚本不写用户工程；插件注册复现仅改变该临时 Python 进程的内存。

### 总问题索引

工期为熟悉当前代码的工程师完成实现、focused regression及review的粗估人日；外部模型/打包依赖不计入，需设计确认后重估。共14项问题/性能优化 + 6项功能缺口，编号在本节点审查报告中唯一。

| 编号 | 优先级 | 标题 | 证据 | 粗估 / 前置依赖 |
|---|---|---|---|---|
| N-01 | P1 | 表达式提交失败丢失动画且无撤销 | A | 1–2日，立即处理 |
| N-02 | P1 | 同节点表达式依赖编辑顺序 | A | 2–3日，与N-03同批 |
| N-03 | P1 | UUID别名/本地循环绕过提交校验 | A | 1–2日，与N-02共用resolver |
| N-04 | P1 | loop/valueAtTime设置时删除采样轨道 | A | 3–5日，依赖N-01–03统一政策 |
| N-05 | P1 | 动画子Transform丢失父变换 | A | 1–2日，唯一参数解析层 |
| N-06 | P1 | 时间节点未冻结动画/序列/时间线源 | A+B | 5–8日，依赖N-05及统一求值帧 |
| N-07 | P1 | 共享源的不同时间分支读同帧 | A | 短期诊断1日，正确求值3–5日依赖N-06 |
| N-08 | P2 | 连续Retime折叠产生整数错帧 | A | 0.5–1日 |
| N-09 | P1 | Cryptomatte浮点epsilon导致误选 | A+B | 2–3日，与N-10同批 |
| N-10 | P1 | 合法Cryptomatte对象hash产生NaN | A | 1–2日，需持久化迁移 |
| N-11 | P2 | Cryptomatte合法JSON错误结构崩溃 | A | 0.5–1日 |
| N-12 | P2 | 表达式负数真值和短路错误 | A | 0.5–1日 |
| N-13 | P2 | 卸载插件仍遗留核心节点注册 | A | 2–3日，registry生命周期事务 |
| N-14 | P1优化 | 高关键帧量UI snapshot超过帧预算 | A并发初测 | 3–5日，最终独占重测；依赖N-05/06 |
| NG-01 | P2 | 4个AI节点仍为占位实现 | G | 按提供商与节点分别估算 |
| NG-02 | P2 | Retime默认光流选项没有实际插帧 | G | UI能力提示0.5日；后端3–5日依赖N-06 |
| NG-03 | P2 | 人脸检测及皮肤mask链未接线 | G+B | 5–8日，依赖真实模型可用 |
| NG-04 | P3 | 文档问号条件语法不可运行 | G | 修正示例0.5日或完整parser1–2日 |
| NG-05 | P2 | Volume仅CPU npz吸收MVP | G | UI提示0.5日；完整GPU/VDB先做设计 |
| NG-06 | P2 | Roto仅前16点提供动画通道 | G+B | 3–5日，stable point ID及schema迁移 |

### 实际节点范围

读取当前 registry 得到 99 个键，包括 merge/blend 的同类别名、color_input 兼容键以及可选 motion_graphic 空注册键。普通分类菜单为下列 16 类。额外发现并成功加载官方 vintage_film / posterize 2 个插件，共 vintage_tone / film_grain / posterize 3 个节点；插件加载错误为零。完整参数/端口清单保存在 /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_node_inventory_20261014.json。该清单证明声明、实例化和端口合同已检查，不能等同所有节点完成 GPU 像素验收。

| 类别 | 数量 | 当前节点 |
|---|---:|---|
| Input | 7 | image_input, constant, video_frame, timeline_clip_input, text, noise, ramp |
| Output | 2 | output, file_output |
| Keyer | 7 | keyer, chroma_keyer, luma_keyer, difference_keyer, light_wrap, cryptomatte_select, matte_cleanup |
| Merge | 9 | blend, merge, deep_merge, keymix, premult, unpremult, matte, dissolve, switch |
| Transform | 5 | transform, reformat, crop, cornerpin, tracker |
| Channel | 5 | shuffle, copy_channel, channel_merge, set_alpha, remove_channels |
| Color | 8 | grade, color_correct, cs_transform, huecorrect, lut, topos_color, colorlookup, cdl |
| Filter | 20 | lens_distortion, mesh_warp, blur, glow, denoise, grain, vectorblur, degrain, sharpen, erode_dilate, defocus, edge_detect, relight, zblur, fog, godrays, chromatic_aberration, vignette, lens_flare, deflicker |
| Mask | 4 | mask, mask_merge, roto_shape, depth_matte |
| Paint | 1 | paint |
| Particle | 1 | particle |
| Utility | 6 | invert, clamp, normalize, compare, dot, noop |
| Time | 4 | time_offset, frame_hold, frame_interpolate, retime |
| AI | 8 | ai_generate, ai_gen_expand, ai_inpaint, ai_expand, ai_remove, ai_denoise, face_track, face_smooth |
| Group | 2 | group, precomp |
| 3D | 8 | scene3d, camera3d, light3d, geo3d, model3d, deformer3d, pointcloud3d, volume3d |

### 可复现问题与详细解决计划

| 编号 | 严重性 / 证据 | 问题、触发步骤及结果 | 根因与精确位置 | 解决方法 | 验收标准 |
|---|---|---|---|---|---|
| N-01 | P1 / A | **被拒绝的表达式编辑会丢失原动画，且不能撤销。** Transform t.translate_x 先设两关键帧，再输入 t.translate_x+1；API 返回 Circular dependency detected，原 keyframe_tracks 却已清空。UI失败分支只刷新面板并返回，不创建 UndoAction。修改已存在表达式为循环表达式也会直接删除原表达式。 | [node_engine.py:903](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:903>) 先调用 node.set_expression；[base_node.py:866](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/base_node.py:866>) 删除轨道；[node_engine.py:917](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:917>) 发现环后仅删新表达式，没有回滚；[composite_page.py:6510](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:6510>) 失败即返回。 | 优先在脱离 live node 的临时依赖图中验证语法/参数/循环，成功后一次提交。或建立完整参数动画事务，失败时恢复旧表达式、启用状态、binding、轨道、revision、manager边。UI失败不能污染工程脏状态。 | 对有轨道/有旧表达式/有绑定的参数分别提交非法循环，完整 before/after 序列化相同；参数及关键帧无丢失；失败不入 undo，随后合法修改 undo/redo 正常。 |
| N-02 | P1 / A | **同节点 this 参数表达式依赖插入顺序，结果错误且无诊断。** 同一 Transform 先设 x=this.translate_y*2，再设 y=10，x=0/y=10；反向设置得到 x=20/y=10。同图语义相同，结果不同。 | [expression_parser.py:967](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/utils/expression_parser.py:967>) 仅建立外部 dep.node_id，明确排除 this.xxx；本地表达式没有拓扑边。[node_engine.py:1576](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:1576>) 每次重建的 manager沿插入顺序处理零入度表达式。 | 将 this.foo 和合法裸参数名统一解析为 (当前 canonical node_id, foo) 依赖；参与拓扑排序、脏传播和循环检测。先计算 base/keyframe，随后严格依赖顺序解析表达式。局部 width/height要区分真实参数和画布别名。 | 同节点多级 x→y→rotation依赖，在各种编辑/序列化字段顺序下结果一致；动画 y 在任意 frame 下被 x读取；同节点环被提交前拒绝。 |
| N-03 | P1 / A | **本地环与 n_ UUID别名环绕过提交时检测。** b.radius绑定 a.radius；给 a.radius设置 n_b.radius，API返回成功，snapshot随后两参数同时报 Circular expression dependency并回落静态值。this.translate_x+1同样提交成功且预览为1，无runtime错误，但preflight将它判环。 | [node_engine.py:850](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:850>) 以真实ID为起点；live manager边却保存 n_a 别名。[expression_parser.py:967](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/utils/expression_parser.py:967>) 不计本地参数；[graph_preflight.py:931](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/graph_preflight.py:931>) 使用另一套本地环规则，三条路径不一致。 | 引入唯一 canonical reference resolver：ID、n_ID及可用节点名先映射真实ID；提交校验、snapshot manager、preflight、binding、脏传播共用同一依赖图。本地环规则配合 N-02 一起修复。 | 普通ID/数字开头UUID/n_UUID/this引用交叉构环全部被拒；UI预览/脚本编辑/导出preflight给同样错误；无假成功、无静态回落遮掩。 |
| N-04 | P1 / A | **loopIn/loopOut/valueAtTime与关键帧互斥政策冲突，循环动画实际不可用。** x设置0帧0→24帧24后提交 loopOut("cycle")，轨道被删除，帧0/12/24/36全部为0。valueAtTime只读取该参数轨道，正常编辑路径也在读取前把它删掉。 | [base_node.py:863](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/base_node.py:863>) 同参数表达式强制移除关键帧；[node_engine.py:1594](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:1594>) sample_value读取刚被删除的轨道；loop_value同样读取node.keyframe_tracks。[base_node.py:1636](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/base_node.py:1636>) 加载时反过来删除表达式。 | 建立“静态/关键帧形成 base，表达式可覆盖 base”政策，value/valueAtTime/loop采样明确读取pre-expression轨道，支持表达式禁用后恢复base。同步Node、serializer迁移、timeline policy及UndoAction；不得只改运行函数。旧互斥工程保持兼容。 | 正常UI创建0→24线性轨道再设loopOut，36帧为12；禁用表达式36帧回base24；valueAtTime(0.5)在24fps返回12；保存重开、undo/redo、预览导出一致。 |
| N-05 | P1 / A | **动画子节点在渲染时丢失父变换。** 父Transform x=100；子Transform x关键帧0→10；帧5 snapshot子x=105，真实BaseEvaluatorMixin创建渲染节点x=5。 | [node_engine.py:1709](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:1709>) snapshot已合成世界参数；[base_evaluator.py:3210](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/evaluators/base_evaluator.py:3210>) 再插值原轨道并优先覆盖snapshot参数，忽略父变换。 | 选定唯一参数准备层：建议snapshot保存已解析参数并标记解析版本/求值帧，renderer不再次对同参数插值。需要子帧重采样时用同一参数评估服务，完成base、表达式、parent全过程。 | 静态/关键帧/表达式父子组合，snapshot参数与实际uniform一致；上述帧5 GPU图像平移105像素；跨帧、保存、预览导出均一致。 |
| N-06 | P1 / A+B | **时间节点不冻结上游动画，且覆盖的视频源类型不完整。** Constant→动画Transform(x每帧+1)→FrameHold(5)→Output，当前帧20有override[Transform]=5，但snapshot与实际ephemeral x都20。image_input序列、timeline_clip_input、Text/Noise/Particle也没有读取node_frame_overrides。 | [node_engine.py:1561](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:1561>) 先按composition frame插值；[node_engine.py:1915](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:1915>) 之后才计算remap。[base_evaluator.py:556](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/evaluators/base_evaluator.py:556>) 只把map放ctx；源侧消费者当前只有 [video_frame_node.py:1817](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/video_frame_node.py:1817>)。 | 将求值请求定义为(node_id, requested_frame, output_port)。时间节点pull上游映射帧，参数、文字/噪声/粒子、序列路径、时间线provider与gate全部读取node requested_frame；共享统一时间上下文。先覆盖image序列/timeline/关键帧，随后确定粒子模拟状态缓存与重采样政策。 | 对视频、图片序列、时间线片段、关键帧Transform、文字动画、噪声/粒子各测试FrameHold/Offset/Retime；hold5从任意comp帧得到同一整幅上游结果，预览导出一致；动态表达式frame/time读取同一局部求值帧。 |
| N-07 | P1 / A | **一个源馈入两个不同时间分支时两分支都读取同一错误帧。** video→offsetA(+5)、offsetB(+9)→Merge，在comp10需要源15和19；snapshot只有video=19，两分支最终共用同一源结果。 | [time_remap.py:296](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/time_remap.py:296>) 冲突first-wins；[node_engine.py:1919](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:1919>) 调用没有传conflict_logger。注释将它说明为v1局限，但用户功能结果仍不正确。 | 配合N-06改为每个(branch,frame)请求独立求值、同帧同源可共享；cache key含requested_frame，不以node_id唯一缓存输出。短期在preflight及节点UI明确阻止冲突，并引导复制source为两个独立源，禁止静默first-wins。 | 两支+5/+9准确显示15/19；reverse、hold、precomp交叉分支也正确；同源同帧只解码一次。临时阻止方案须出现可定位诊断，导出不得默默生成错帧。 |
| N-08 | P2 / A | **连续Retime合并丢失逐级round导致错帧。** TimeRemap(0.5).pushed_upstream(2.0)将速度合为1；帧1顺序结果0，合并结果1；帧3顺序4，合并3；帧5顺序4，合并5。 | [time_remap.py:145](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/time_remap.py:145>) 明确使用近似的乘法折叠；整数round的映射不满足仿射闭包。 | 非整数速度一律保留inner链，逐级apply。只有证明整数映射且没有中间round损失时折叠。另一方案全链保持连续时间并在源统一采样，但需明确改变语义并迁移旧工程。 | 穷举速度0.1/0.5/1.25/2/3、offset±、hold/reverse/loop，组合apply等于逐级apply；长链预览导出索引一致。 |
| N-09 | P1 / A+B | **Cryptomatte float ID被绝对epsilon误去重/误选。** 对真实object_0与object_3，float IDs分别8.4259e-36及-2.8745e-34；先后选两对象最终只保留object_0。shader float_r比较差<1e-6，会将大量不同tiny hash连同背景0一起误选。 | [cryptomatte_select_node.py:131](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/cryptomatte_select_node.py:131>) Python去重1e-9；remove/toggle同政策；[shaders.py:6883](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6883>) 浮点差比较；[shaders.py:6899](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6899>) float_r epsilon1e-6。 | ID视为bit-pattern而不是连续测量数。Python使用struct转换uint32作增删toggle键；GPU使用floatBitsToUint或等价位比较，uint uniform传选中值。ID纹理使用NEAREST及R32F/RGBA32F保持位模式，禁止色彩变换/half转换。rgba24继续整数比较。 | 上述两个ID可同时选中并分别取消；GPU矩阵含正/负/tiny/large/background0，只目标ID区域为白；导入EXR与保存重开位模式不变。 |
| N-10 | P1 / A | **Cryptomatte哈希可产生NaN，使合法对象无法选择并污染JSON。** cryptomatte_float_id("object_440")为NaN；object_572、object_924同样。NaN与自身比较不相等，abs比较永远false，json默认将其写成非标准NaN。 | [cryptomatte.py:65](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/cryptomatte.py:65>) 对原始MurmurHash直接reinterpret浮点；[cryptomatte.py:84](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/cryptomatte.py:84>) entry重复同逻辑；[cryptomatte_select_node.py:123](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/cryptomatte_select_node.py:123>) 持久化普通float。 | 建立单一hash→安全float转换服务，将非法指数模式转换为有限浮点并保持稳定；manifest、生成AOV、selection remap必须使用同一服务。持久化推荐uint32/8位hex字符串并设schema版本；读取旧float格式可迁移。写JSON用allow_nan=False并给出领域诊断。 | 10,000个对象名全得到有限唯一ID；object_440可选中/取消/保存重开；manifest与AOV同ID；无NaN/Infinity JSON。 |
| N-11 | P2 / A | **Cryptomatte可选持久化JSON只验证语法，不验证结构，工程载入/求值会抛异常。** selected_ids="null"或selected_names="null"调用getter抛TypeError；manifest="[]"抛AttributeError。 | [cryptomatte_select_node.py:100](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/cryptomatte_select_node.py:100>) loads后直接遍历；[cryptomatte_select_node.py:114](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/cryptomatte_select_node.py:114>) 直接data.items()。 | JSON schema验证（ids=list finite IDs，names=list str，manifest=dict hex→str）；统一解码函数返回结构化错误而不是异常；无效state保留工程原payload以便修复并在节点诊断，禁止宽泛吞异常。修remove_selection同时按zip索引ID/names，避免不等长时越界。 | null/[]/{}/字符串/非有限id/不等长名单均不崩；可定位到参数；合法选区功能正常；有错误时导出明确阻止或要求修复。 |
| N-12 | P2 / A | **表达式负数真值及短路逻辑错误。** "10 if -1 else 20"得20；"(-1) and 1"得0；"1 or sqrt(-1)"以及"0 and sqrt(-1)"竟求值不该求的右支并报domain error。这会让用于保护除零/越界的条件表达式失效。 | [expression_parser.py:524](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/utils/expression_parser.py:524>) 先求全部BoolOp operands并用v>0判真；[expression_parser.py:535](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/utils/expression_parser.py:535>) IfExp同样v>0，Unary not则使用Python bool语义，内部不一致。 | 统一数值真值规则(nonzero为真)；BoolOp逐项短路，and遇false停止、or遇true停止；保持返回0/1合同。Compare链也采用短路。可分别测试本地与graph context。 | 负数为true、0为false；前述and/or不执行不可达sqrt；条件保护1/x在x=0时不报错；Unary not/IfExp/BoolOp语义一致。 |
| N-13 | P2 / A | **插件卸载不更新合成核心注册表。** register_plugin_nodes(manager)成功后manager.unregister_plugin("audit_temp_plugin")，manager已无节点但NODE_REGISTRY仍是AuditNode，create_node_by_type继续成功。实际on_unload资源可能已关闭，后续节点仍可创建。 | [node_plugin.py:478](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_plugin.py:478>) 只清manager局部注册；[node_engine.py:2893](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:2893>) 复制注册到全局但无对应卸载同步；[node_engine.py:2975](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:2975>) factory优先旧全局class。 | Registry保留plugin owner和override历史，加载/卸载/reload走统一事务及生命周期事件；所有NODE_REGISTRY/NODE_CATEGORIES/capabilities/UI菜单同时更新。卸载被当前工程使用插件时先诊断，保留UnknownPlaceholder及payload，按render线程清理持久实例。 | 卸载后菜单与factory均不再创建；重载使用新class；覆盖的内置节点恢复原class；工程重开保留参数并明确missing plugin；预览取消/资源释放无泄漏。 |
| N-14 | P1优化 / A（并发初测） | **大量关键帧时UI线程每帧snapshot复制及轨道重建超过帧预算。** 合成50 Transform，5轨道/节点、120键/轨道（30,000键），snapshot中位118.593ms/max168.117ms；100节点60,000键中位247.773ms/max362.762ms。静态50节点仅2.516ms。无解码、无GPU。**初测与root pytest同机并发，需以独占重测为最终数值**。 | [node_engine.py:1548](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:1548>) 每轨道反复get_param_defs并反序列化；[node_engine.py:1574](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:1574>) 即使无表达式也deepcopy全部params为alias；[node_engine.py:1826](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:1826>) 每帧deepcopy所有轨道；snapshot要求UI线程。renderer另重复插值，见N-05。 | 按track revision缓存不可变已编译轨道与参数schema；每帧只二分采样并复制变化参数，轨道/端口/拓扑共享不可变对象；只为存在表达式引用的节点构建alias；表达式AST/依赖图随revision复用。先做正确性N-05/N-06再删除重复求值。必要时在worker计算参数，UI只提交轻量请求。 | 独占同机器同图重复10次，报告median/p95/分项profile；50节点30,000键snapshot目标≤10ms（或项目明确预算），参数/undo/持久化不改变；4K playback增加动画后UI不被每帧>100ms阻塞。 |

### 功能不足与建议（与bug分开）

| 编号 | 优先级 / 证据 | 当前能力缺口及用户影响 | 具体补齐方法 | 验收标准 |
|---|---|---|---|---|
| NG-01 | P2 / G | AI Generate、AI Inpaint、AI Expand、AI Denoise为显式PlaceholderNode，能加图、调参、保存，但没有效果实现。placeholder preview与export诊断已有政策，不能称为悄悄丢效果bug。参考 [placeholder_node.py:16](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/placeholder_node.py:16>) 与各AI子类。 | 菜单/搜索按available backend给能力状态；将相同功能收敛至已有AI Gen Expand / AI Remove异步入口，避免两组名称混淆。实现要包含schema、provider/model身份、异步progress/cancel、content cache、外发素材审核及导出确定性。未实现选项标“尚未提供”，隐藏不可用参数。 | 菜单能力状态与实际效果一致；占位状态接入输出时诊断明确；上线节点用同素材预览/导出可验证，失败不静默透传。 |
| NG-02 | P2 / G | Retime use_optical_flow默认True，但只是reserved metadata，dispatcher透传没有光流/混帧。慢放会重复帧。参考 [retime_node.py:13](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/retime_node.py:13>)、[retime_node.py:48](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/retime_node.py:48>)。FrameInterpolate是另一个显式双帧节点，并不自动补齐Retime。 | 控件先禁用并解释需要插帧backend；实现时由时间请求服务提供邻帧和fraction，调用现有RIFE管理器/显式blend fallback，保留retime缓存及质量等级，export缺模型不能暗降级。速度关键帧应定义是瞬时播放速度积分还是坐标缩放，以避免speed ramp跳时。 | 0.5x放大移动对象，启用光流确实产生中间位置；开/关有可测像素差；模型不可用显示降级原因；预览导出同策略。 |
| NG-03 | P2 / G+B | Face Track无生产detector接线：全仓set_face_detector只有定义；face_index仅声明/默认没有消费者。rerun_on_evaluate调用detect_at未传图像。UI审查agent同时确认没有face Analyze或skin-mask专用接线。用户可添加节点，但当前只可作为已有landmark数据容器，无法正常从素材检测再磨皮。参考 [face_track_node.py:36](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/face_track_node.py:36>)、[face_track_node.py:178](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/face_track_node.py:178>)。 | 与ModelManager及AIComputeManager接入真实face detector，输入正确帧array，独立worker批分析并提交landmark immutable track；明确多脸face_index及持续身份追踪；新增Skin Mask source/输出供Face Smooth连接；无模型时禁用rerun/Analyze并可见诊断。 | 测试视频可选人脸序号→Analyze→逐帧landmarks→mask→Face Smooth完整链；多脸不串人，mask排除眼/嘴，撤销/取消/保存重开有效；UI无同步推理。 |
| NG-04 | P3 / G（文档与实现不符） | ExpressionParser文档声称支持 condition ? a : b，但当前直接Python ast.parse无转换，官方示例input.width > 1000 ? 1.0 : 0.5被拒。[expression_parser.py:11](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/utils/expression_parser.py:11>)、[expression_parser.py:247](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/utils/expression_parser.py:247>)。 | 实现带括号/嵌套的token parser将问号条件式安全转换为IfExp，或统一产品提示为Python a if condition else b并迁移所有示例。不要用单个正则处理嵌套表达式。 | 所有展示给用户的示例可直接运行；nested ternary、负数条件、错误位置提示均正确；若不支持问号语法，UI和文档明确说明。 |
| NG-05 | P2 / G | Volume3D当前是.npz CPU正交单轴吸收积分，暂不支持OpenVDB、GPU Scene3D体积交互、温度/发光/多散射；是文档承认的MVP，不应作为bug。参考 [volume3d_node.py:1](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/volume3d_node.py:1>)。 | 菜单将“CPU volume MVP/仅npz”能力显示清楚；先做OpenVDB依赖打包与文件尺寸限制，再GPU 3D texture/raymarch、相机/灯光payload、density/emission网格和资源生命周期。CPU降采样仅preview，最终质量有明确选择。 | .vdb未支持时给可见诊断而非空帧；上线后体积随相机与Scene3D场景变换；preview/export质量政策可解释，4K长序列内存上限可测。 |
| NG-06 | P2 / G+B | Roto路径数值动画通道只暴露前16点，复杂轮廓17点以后不能用同一关键帧合同逐点动画。paths仍能保存更多点，UIviewport为所有点建handle。参考 [roto_shape_node.py:100](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/roto_shape_node.py:100>)、[roto_shape_node.py:119](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/roto_shape_node.py:119>)。这项为当前上限及设计不足，未断言整幅GPU轮廓也只渲染16点。 | 用stable point ID取代易位移的index参数；动态schema覆盖全部点并明确资源上限；增删点在command中迁移对应轨道/绑定，按段自适应细分与GPU缓冲存点，避免全uniform固定数组。短期16点限制必须在UI提示。 | 32/64点轮廓可逐点动画，增删点后其余轨道仍附着原点；超过上限明确提示；保存重开、undo/redo、预览导出无错点。 |

### 推荐顺序与覆盖边界

第一批先修N-01防止数据丢失，并将N-02/N-03/N-04整合为统一参数动画/表达式合同。第二批修N-05/N-06并确立唯一求值帧，再处理N-07/N-08时间分支与组合；不能先仅优化缓存，否则把错帧缓存得更快。第三批修N-09/N-10/N-11 Cryptomatte位模式与持久化。第四批以独占重测修N-14，随后插件生命周期N-13和逻辑N-12。功能缺口依可用模型/backend排期。

本报告未把“HDR任意clamp”“大量except”“历史TODO”等模式本身列为bug。未对所有3D材质、外部AI模型、EXR/deep多采样、全部LUT组合做像素验证，也没有假称对这些完成整页UI操作。已检测的Placeholder治理、官方插件基本加载、graph连接校验、metadata字段保留不属于此次新问题。


## GPU、色彩、导出与3D详细计划

本报告由根审计的渲染子任务产生，未修改产品源码。范围：PyQt6 + ModernGL + GLSL 合成预览、图片/视频/图片序列导出、GPU 资源生命周期、缓存与 3D 材质路径。遵循 `.clinerules`；微型 GPU 复现仅在专属工作线程创建/操作独立 GL context。根任务负责完整测试套件、测试视频与 2K/4K 基准，本文不重复该负载。

### 证据口径与复现文件

- **A-GPU**：真实 ModernGL / 生产 GPUResourcePool，8×8 像素，独立渲染线程运行。
- **A-CPU**：真实 numpy / PyAV 色彩转换，没有打开视频编码器或产生交付视频。
- **A-注入**：调用真实生产控制流程，以 fake 解码帧、fake 编码器或保存器精确注入失败/取消；证明错误处理合同，不声称已用真实素材触发同一故障。
- **B-静态**：完整调用链和具体源码可证，但尚未在完整应用交互或真实模型上做视觉对照。需按验收标准复验。
- **C-能力不足**：当前明确未接线或不支持的能力，和 bug 分开记录。

可独立复运行的文件：

1. `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_render_repro_20261014.py`，结果 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_render_repro_20261014.json`：Paint/AI 真实 GPU 上传；Paint alpha；图片 P3 转换；项目快门；序列取消、丢帧、cleanup、并行启动失败。
2. `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_video_color_repro_20261014.py`，结果 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_video_color_repro_20261014.json`：实际 PyAV YUV 色域/范围转换；10-bit 前置量化；全黑成功。
3. `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_volume_repro_20261014.py`，结果 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_volume_repro_20261014.json`：文件修改后体积缓存不更新（纹理上传器为 fake；体积计算与节点签名为真实生产逻辑）。

4. `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_aov_repro_20261014.py`，结果 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/focused_repro/topos_composite_aov_repro_20261014.json`：AOV不做OETF，但PNG/TIFF量化前仍裁切signed/>1数据。

执行：项目 `.venv/bin/python <绝对脚本路径>`。单脚本耗时约 0.8–1.9 秒；所有输入均在临时目录，未修改测试素材。视频色彩脚本的 fake 编码器只采集提交帧，结果中的 `/tmp/not-created-*.mp4` 不是真实交付文件。

### 问题计划总表

| ID | 优先级建议 | 功能/问题 | 证据 | 影响 | 处理方向 | 建议工时 |
|---|---|---|---|---|---|---|
| R-01 | P1 | Paint/AI CPU float32 写入默认 float16 纹理，功能失效 | A-GPU | Paint 静默直通，AI 结果无法回到图中 | 上传统一按目标 dtype 转换，失败归还纹理 | 1–2 天 |
| R-02 | P1 | Paint 对 straight-alpha 输入把 alpha 也预乘，alpha 被平方 | A-GPU（隔离 f4） | 半透明素材全图 alpha 错，边缘变淡 | 只预乘 RGB、保留 alpha | 0.5 天 |
| R-03 | P1 | 合成视频 limited 范围重复压缩 | A-CPU | 默认 Rec.709 黑位抬高、白位降低 | RGB 中间值 full range；YUV 层统一压 legal | 1–2 天 |
| R-04 | P1 | BT.2020 编码辅助函数把 BGR 当 RGB | A-CPU | HDR10/HLG 红蓝色相和亮度错 | 辅助函数声明通道序，进入前转换 | 0.5–1 天 |
| R-05 | P1 | P3/BT.2020 图片序列转换对 BGR 用 RGB 原色矩阵 | A-CPU | 广色域图像颜色错，和视频路径不一致 | BGR↔RGB 只围绕色彩转换前三通道 | 0.5–1 天 |
| R-06 | P1 | 广色域 SDR / ProRes 10-bit 提交前已量化成 8-bit | A-注入 | 10-bit 文件仍带 8-bit 色阶断层 | 高位深按目标 pix_fmt 判定，提交 uint16 | 1 天 |
| R-07 | P2 | 自定义 HDR 描述符被固定 HDR10/HLG 标签覆盖 | B-静态 | P3+PQ / full-range HDR 像素与标签不符 | 元数据从描述符构建，HDR static metadata 单独添加 | 1 天 |
| R-08 | P1 | 中途取消已写一帧就报告完成 | A-注入 | 不完整视频/序列被视为成功，可能交付 | cancelled/partial/complete 分离 | 1 天 |
| R-09 | P1 | 视频全部渲染失败仍导出全黑并报告成功 | A-注入 | 错误成果被交付，丢帧数 UI 不可见 | 默认最终导出失败；显式允许补帧策略 | 1–2 天 |
| R-10 | P1 | 图片序列渲染/写盘失败跳过却成功 | A-注入 | 缺帧序列且进度/状态失真 | 失败帧中止或给明确 partial 状态 | 1 天 |
| R-11 | P1 | 串行图片序列缺少 GPU / 解码资源 finally 清理 | A-注入+B | 完成、取消、异常时持有资源；窗口/线程关闭风险 | 序列全入口 finally cleanup | 0.5–1 天 |
| R-12 | P2 | 并行序列 context 工厂失败不会回退串行 | A-注入 | 可串行导出的工程报 Render failed | 接入 factory_fallback 与原错误 | 0.5 天 |
| R-13 | P1 | 导出重建 eval context 丢失项目运动快门 | A-注入 | VectorBlur 预览/导出不同 | context 创建后注入 + 快门依赖进 cache key | 1 天 |
| R-14 | P1 | 导出遗漏全局 3D Scene Settings | B-静态 | 相机、光照、背景、输出开关回落旧默认 | 不可变 render context 带工程 3D 输出设置 | 2–3 天 |
| R-15 | P1 | 页面渲染输出读回固定丢弃 alpha | B-静态 | PNG/TIFF/EXR 无透明，预乘边缘暗 | BGRA/RGBA 读回合同、通道/预乘选项 | 2–3 天 |
| R-16 | P1 | 3D 材质不透明度 / alpha mask 无渲染作用 | B-静态 | 材质面板调透明无效，玻璃预设不透明 | alpha 因子、贴图 alpha、blend/mask 与 depth 策略 | 3–5 天 |
| R-17 | P1 | 有贴图时基色、金属、粗糙度 scalar 被覆盖 | B-静态 | 材质面板调值不起作用，模型外观不符 | 贴图与 scalar 按合同相乘，override 模式明确 | 1–2 天 |
| R-18 | P1 | 材质贴图色彩空间丢失、线性输入二次 gamma | B-静态 | linear EXR / 图节点 override 材质被误解码 | 保留 descriptor、按 slot 色彩空间解码一次 | 2–3 天 |
| R-19 | P1 | metallic_in 与 roughness_in 争用同 ORM sampler | B-静态 | 同时连接只剩一张纹理，参数彼此污染 | 分离 sampler 或显式 GPU 打包 ORM | 1–2 天 |
| R-20 | P2 | 材质缓存未按 program 区分 uniform 状态 | B-静态，需模型复验 | 同材质不同 shader 变体可能丢贴图/自发光 | program identity 纳入缓存或切换重置 | 1 天 |
| R-21 | P2 | 体积文件改了仍复用旧 GPU 内容 | A-注入 | 密度文件热更新不反映 | 内容签名加已解析文件 stat | 0.5 天 |
| R-22 | P1 | 动态噪声/粒子/帧表达式被判为静态，只能导出单帧 | A-CPU+B入口 | 动画图无视频/序列出口 | 统一 frame-dependency capability，不列少数类型 | 1–2 天 |
| R-23 | P1 | position/motion等数据AOV导出PNG/TIFF裁切有效数值 | A-CPU | 负向运动/世界坐标丢失，外部重建不可用 | AOV数据合同限制格式/显式可逆编码 | 1–2 天 |

依赖：R-02依赖R-01先解除上传失败遮蔽；R-03/04/05/06/07共用输出色彩合同，应一起回归；R-08/09/10共用导出状态与完整性；R-15/23共用通道/数据读回；R-16/17/18/19共用材质能力/schema；R-20待混合shader模型复现确认再实施。

P1 为明显功能失效/错误输出，建议优先修复后再做性能优化。工时是单工程师实现与 focused regression 的粗估，不含完整跨平台验收，根计划可按依赖重新排期。问题不应仅在测试 harness 中修；必须覆盖真实 GPUResourcePool、真实编码器以及 UI 状态消费。

### 详细问题、解决步骤与验收

#### R-01：Paint 和 AI 结果上传的数据位宽不匹配

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/evaluators/filter_evaluator.py:986`、`:998`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/evaluators/base_evaluator.py:1667`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/gpu_resource_pool.py:964`。

**前提/症状**：任意存在笔触的 Paint，或 AI remove 等命中 CPU 结果并回写 GPU。`acquire_texture()` 默认为 `f2`（RGBA 每像素 8 字节），上传数组为 float32（每像素 16 字节）。真实 8×8 生产 pool 得到 `_moderngl.Error: data size mismatch 1024 != 512`，Paint 返回输入直通；AI `_upload_ai_result_texture()` 返回 None。失败时先取得的纹理也没有归还 pool。测试 `test_paint_node.py` 使用自定义 `_Pool.acquire_texture(... dtype="f4")`，掩盖生产默认值。

**解决**：1. 统一 CPU→纹理上传 helper，从目标 `tex.dtype` 或 render_mode 明确选 numpy dtype。2. 需要 f32 fidelity 的导出走 explicit `dtype="f4"`，预览可转 float16。3. Paint result 和 mask 两个输出同修；AI 缓存、clone/erase/solid 全覆盖。4. 获取成功、write 失败时 finally 归还新 texture，输出 structured diagnostic，不能仅日志+直通。5. 用真实生产 pool 替换/补充测试里的 fake f4 pool。

**验收**：8×8 和真实 2K 笔触可见；mask 有正确覆盖；AI 缓存命中能输出；无 data-size 异常；100 次失败注入后 active_count 不累增；预览/导出各验证一次。

#### R-02：Paint 预乘时 alpha 被平方

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/evaluators/filter_evaluator.py:948`。

**前提/症状**：straight-alpha 输入（如半透明 PNG/Constant）接 Paint，只需存在一个笔触，即使笔触不覆盖某像素。代码对全 RGBA 做 `img * img[...,3:4]`。隔离 f4 上传后的真实 GPU 读回：输入 `[0.2,0.2,0.2,0.5]`，未画区域输出 `[0.1,0.1,0.1,0.25]`，正确为 `[0.1,0.1,0.1,0.5]`。R-01 当前会掩盖它；修 R-01 后仍必须修本项。

**解决**：只对 `img[..., :3]` 乘 alpha；alpha 保持原值；clone/erase/solid 在预乘域执行。避免修改输入只读 view。统一 premult/straight helper，并保存 `alpha_premultiplied=True`。

**验收**：alpha 0、0.5、1 的未画区域原 alpha 不变；正常笔触计算符合 over；半透明输入接 Merge 与直通对照边缘一致。

#### R-03：视频 limited range 重复压缩

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:545`、`:1005`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/export/video_encoder.py:2082`、`:2144`；provider range 在 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/color/color_transform_provider.py:542`。

**前提/症状**：默认 REC709_VIDEO 即 limited-range 输出。合成先 transform_color 到 limited RGB 信号，再乘 255；encoder 明确认为输入 RGB full range（src JPEG），再次压成 YUV MPEG legal。实际 PyAV 转换：线性黑 → RGB 16 → Y=29，线性白 → RGB235 → Y=217；正确 Y 黑白为 16/235。HDR BT.2020 numpy 路径同样将先压过的 RGB 再压一次。

**解决**：1. RGB 中间 descriptor 与最终容器 descriptor 分开，RGB 的 primaries/transfer 相同但 range=FULL。2. YUV 打包层唯一负责 legal range；stream tag 保留用户目标 range。3. 串行、并行、图片与视频 helper 不共用含混 range 合同。4. 实际编码后解码测 black/white、灰阶/色条，防止只断言标签。

**验收**：默认 8-bit Rec709 黑/白 Y16/235；10-bit limited Y64/940（允许编码器误差）；full-range 0/max；黑到白渐变不二次缩窄；serial/parallel 相同。

#### R-04：BT.2020 编码辅助函数通道序错

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/export/video_encoder.py:2064`、`:2126`；合成提交 BGR 在 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:550`。

**前提/症状**：合成 HDR10/HLG 提交 bgr48le。`_convert_packed_frame` 最后把 original `data` 交 `_convert_rgb_to_yuv_bt2020`，后者按第 0/1/2 通道为 r/g/b。真实 PyAV 辅助转换：红 BGR `[0,0,65535]` 的 limited 10-bit Y=116，而正确 RGB 同色 Y=294。绿不变，红蓝交换，造成部分测试只用灰色时不暴露。

**解决**：BGR 入辅助函数前显式前三通道转 RGB；或 helper 接 `source_format` 并内部统一。uint8、uint16、rgb/bgr 都有同义测试，alpha 当前 unsupported 必须保持显式诊断。

**验收**：BT.2020 原色条实际 encode→decode与参考一致，红 Y约294、蓝 Y约116；sdr wide-gamut BT.2020 同修；不能只读取 primaries tag。

#### R-05：图片序列原色转换对 BGR 用 RGB 矩阵

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:1384`；原色矩阵函数 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/color/color_transform_provider.py:469`。

**前提/症状**：P3/BT.2020 等跨原色的 PNG/TIFF 序列。注释称“channel-order agnostic”，实际矩阵有 RGB 语义。P3 红色 BGR16 实际 `[0,0,62886]`，正确 `[9080,13125,60127]`，通道最大偏差13125/65535。视频路径已在 transform 前后 swap，图片路径遗漏。

**解决**：包装“仅前三通道”的 BGR→RGB→transform→BGR；若升级 BGRA，要保持 alpha 末位。删掉误导注释，provider 合同固定 RGB，输入语义由 adapter 处理。

**验收**：红绿蓝、非对称色卡及灰色矩阵对照；BT709→P3→BT709 回环；图片与视频同 descriptor 保持一致。

#### R-06：10-bit SDR 被预先量化 8-bit

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:411`、`:551`、`:1009`；对话框 codec pix_fmt `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_render_dialog.py:89`。

**前提/症状**：Display P3 SDR 自动升级 yuv420p10le，或 ProRes 422 10-bit。CPU打包只按 `is_hdr` 决定 uint16，所以 wide-gamut SDR 仍为 uint8 bgr24。1024级 P3 渐变仅剩237个唯一值；10-bit编码器无法恢复丢失色阶。

**解决**：选定 pix_fmt 后解析 bit depth，并设 `high_depth_input`，HDR仅决定transfer/meta；10/12/16位走bgr48le满16bit打包。black placeholder与parallel stage同改。UI明确最终位深，按codec支持预检。

**验收**：1024级无损/高质量渐变实际可分辨 >256码值；ProRes非HDR提交uint16；8bit SDR保持uint8；preview/export量化各有可解释合同。

#### R-07：HDR元数据固定覆盖自定义描述符

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:421`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/codec/color_metadata_config.py:167`。

**前提/症状**：项目偏好支持 primaries/transfer/range 组合，用户选 P3+PQ 或 full range HDR；像素按该descriptor编码，但元数据一律hdr10()/hlg()（BT2020、limited）。此外mastering/MaxCLL固定1000nit和真实HDR范围不必一致。

**解决**：从descriptor构造primaries/transfer/matrix/range，只有适用HDR10时附static metadata；mastering/MaxCLL提供显式目标或从工程设置读取；无可靠数据时不编造实测峰值。无支持codec/pixfmt时预检阻止。

**验收**：P3PQ、BT2020fullPQ、标准HDR10/HLG各自tags与像素同源；输出峰值metadata与目标设置一致；预检列出编码器不支持组合。

#### R-08：取消后不完整成果报告完成

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:573`、`:740`、`:1118`、`:1262`；worker `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_worker.py:136`。

**前提/症状**：点取消时已经写入1帧。`cancelled`只认written==0；parallel直接写success=True。真实生产序列控制流程注入取消：请求5帧、写1帧，返回success=True,error=""，UIfinished路径显示完成。源码注释将“written>0”误称fully-rendered，已有历史测试只证明串并行同错，并不能代表正确交付语义。

**解决**：结果明确status=complete/cancelled/failed/partial；complete必须written==total且无失败。部分文件可保留供续传，但必须带partial状态和帧清单，按用户选项处理；UI不能调用成功toast；取消恰好落在最后完整帧后允许complete。

**验收**：0帧、1/N帧、N/N帧分别验证；串并行语义相同；取消后没有仍运行的编码/渲染线程；未授权覆盖已有成果时避免清理旧文件。

#### R-09：全部帧失败仍全黑成功，诊断未传UI

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:489`、`:1039`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_worker.py:136`；单帧诊断消费者 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_frame_renderer.py:202`。

**前提/症状**：渲染返回None（媒体运行时损坏、GL失败、节点结果错误）。导出用前帧/黑帧占位维持时码，但即使所有3帧均None，仍success=True，frames_dropped=3；worker只把path发finished，用户看不到丢帧/diagnostics。

**解决**：最终导出默认fail-fast并保留明确原错误；“容错补帧”作为显式策略，受丢帧比例/连续失败阈值约束；0成功帧永远failed；Result/worker/UI传递丢帧位置、节点diagnostics、原因。预检已有静态缺媒体阻止，但不能覆盖运行时问题。

**验收**：首帧、中间帧、全帧失败分别注入；默认导出中止；容错策略warning可见且summary列具体帧；正确帧不无故压缩时间线。

#### R-10：图片序列存在缺帧仍报成功

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:704`、`:722`、`:1211`、`:1250`。

**前提/症状**：某帧None或FileIO.save_image=False。串行continue、并行sink return；只要有1张则成功，frames_dropped一直0。注入中间帧失败结果：仅x_0001.png和x_0003.png，success=True,written=2,error=""。parallel consumed进度还可到100%。

**解决**：渲染/保存失败转可辨错误并中止；或显式partial状态保存失败帧列表；结束校验written==requested且文件集合连续；进度区分rendered/encoded/written。写盘用临时文件+原子替换，清理仅本次新文件，避免覆盖老序列后失败删除原成果。

**验收**：None、False、exception、disk-full、权限失败均不给complete；成功文件数和连续编号完整；串并行进度和错误一致。

#### R-11：串行图片序列缺少finally清理

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:690`–`:753`、`:1293`；worker `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_worker.py:71`。

**前提/症状**：parallel_frames=1或parallel退回serial后，创建persistent CompositeFrameRenderer，序列返回没有_cleanup_frame_renderer（视频有finally）；worker.run也没有finally调用。cleanup spy验证返回后cleaned=False且self._frame_renderer仍持有。GC不能代替渲染线程按顺序release纹理/节点/解码session，跨线程析构更不可依赖。

**解决**：序列串行入口try/finally _cleanup_frame_renderer；高层worker也用明确生命周期防早退出（避免重复清理应幂等）。先释放provider/node GPU引用，再销毁own context；wrap主context不能release。并行teardown保持owner-thread。

**验收**：正常、取消、写入False、异常、空range路径各验；resources/decoder session归零；重复20次序列导出resident memory回到稳定基线；不从UI线程清理。

#### R-12：并行图片序列工厂失败没有serial fallback

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:1222`、`:1270`。

**前提/症状**：GL context/VRAM使所有并行worker factory失败。state初始化factory_failures但调用render_frames_parallel未传factory_fallback；计数永远0，fallback分支永远不触发。注入factory失败两次时，错误只有Render failed，serial_calls=[]；video stage有该回调，序列遗漏。

**解决**：接同video的factory_fallback，错误保留首次具体原因；只在0写入且非取消、非shader/graph领域错误的可降级context失败时回退serial；计数在锁下更新；若serial也失败诊断联合呈现。

**验收**：全部factory失败时serial执行且完成；部分worker失败剩余可用worker正常；取消不触发降级；资源释放在创建线程。

#### R-13：Output项目快门导出丢失

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_export_renderer.py:236`、`:184`、`:490`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/vectorblur_node.py:57`。

**前提/症状**：Output.motion_blur_shutter非默认且VectorBlur.use_project_shutter=True。render_and_readback先给旧ctx赋值，随后_ensure_gpu_resources总是_install_eval_context创建新ctx，motion_blur_shutter=None。注入真实render_full_frame流程观测请求0.9，节点effective_shutter回落0.5。第一次没有旧ctx也同错；multi-pass入口根本没有这段赋值。

**解决**：初始化ctx后从当前snapshot注入，统一readback/gpu/multipass；每帧覆盖而不是旧ctx更新。将项目快门作为VectorBlur上下文依赖纳入cache key，Output位于下游时普通dirty传播不会自动使上游VectorBlur失效。

**验收**：项目快门0、0.25、0.9与node自身0.5对照；use_project=False保持自身；改变项目值立刻影响预览/导出并且cache无陈旧结果；full/tile/multipass一致。

#### R-14：导出遗漏工程全局3D设置

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/scene3d_node.py:3595`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_export_renderer.py:184`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_frame_renderer.py:751`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/graph_snapshot.py:102`；预览注入 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_renderer.py:842`。

**前提/症状**：当前Scene3D真实节点是纯renderer，scene3d注释明确节点self.parameters只有旧默认，真实用户参数从ctx.global_3d_scene_settings读。预览request接此dict；GraphSnapshot没有viewer_state/3D设置；FrameRenderer序列化重建只snapshot nodes；ExportRenderer新ctx仅GL/provider/config，因此导出回落旧默认，相机导航/默认灯/背景/IBL/MSAA/output_*都可能与成品预览不同。连接Camera/Light可覆盖部分相机/灯，但不弥补背景/IBL/输出开关。

**解决**：定义不可变CompositionRenderSettings，来自持久化viewer_state.default_3d_scene（只带成品参数，排除临时导航/selection/isolate）；所有导出入口（图片、序列、视频、multi-pass、headless、AI输入）按同合同安装ctx；字段纳入相关cache key。输出_*和show_*预览参考设置分离读取，不能直接复制整个UI状态。

**验收**：工程设置和旧默认差异大的3D场景截图对比（相机yaw、ambient、IBL、bg_opaque、output_grid）；保存重开后相同；connected Camera/Light优先级相同；导出不包含选择轮廓/调试网格。

#### R-15：页面导出固定剥除alpha

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_export_renderer.py:1577`、`:1441`、tiled stitch读取`:1048`；已有RGBA路径 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/file_output_node.py:416`、`:454`。

**前提/症状**：Keyer、Mask、Scene3D透明背景后，从页面渲染输出保存PNG/TIFF/EXR。full/tile CPU结果只BGR3，alpha不可恢复，预乘颜色还可能直接作为不透明色保存，边缘变暗。FileOutput节点支持channels/alpha_mode与RGBA，说明存在解决路径，但不能当页面Render Output已支持。视频opaque格式本就无alpha，不能静默丢弃可承载格式的alpha。

**解决**：readback明确channels/alpha representation，opaque预览仍可3ch；最终透明readback为BGRA float且transfer先unpremult后encode，再按target alpha合同premult；tile stitch保留alpha；UI PNG/TIFF/EXR有通道与straight/premult选择。复用FileOutput转换/写入合同；ProRes4444和透明video仅在encoder支持时开放。

**验收**：透明圆/半透明梯度导出重导入alpha逐像素匹配；PNG棋盘不是烧进图片；EXR保留alpha及>1HDR；premult/straight合成边缘同参考。

#### R-16：材质面板不透明度没有实际渲染效果

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/material_panel.py:86`、`:357`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/material_model.py:599`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6641`；scene material透明排序`:1208`。

**前提/症状**：opacity调到0.3或应用玻璃预设，bridge将alpha字段写入MaterialData，但PBR fragment没有alphauniform，也不读baseColorTexture.a，固定fragColor=vec4(color,1)。beauty绘制循环不按材质透明开blend。MASK不discard；导入材质conversion也没有alphaMode/alphaCutoff/baseColorFactor.a映射。排序函数“透明保持原序”不能实现正确透明。

**解决**：完整OPAQUE/MASK/BLEND合同：因子与贴图alpha，MASK discard cutoff，BLEND预乘输出+ONE/ONE_MINUS_SRC_ALPHA、透明depth-write off/back-to-front sort；双面/culling按材质；导入保留alphaMode/cutoff/factor；AOV/shadow/pick对mask一致。

**验收**：opacity0真的不可见、0.3看到背景、1不变；交叠透明远近顺序稳定；叶片mask无矩形实片；导出与预览相同；shadow/pick不选到透明洞。

#### R-17：有贴图时材质scalar被覆盖

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6516`、`:6525`、`:6633`；scalar uniforms `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/scene3d_node.py:4605`。

**前提/症状**：有base_color、ORM、emissive贴图的模型。shader用贴图值覆盖u_albedo、u_metallic、u_roughness、u_emissive；面板对应scalar虽存储和设uniform，最终不起作用（无贴图时有效）。模型导入的factor语义也未组合，明显影响同贴图多材质。

**解决**：define imported/multiply/override合同；默认纹理×scalar因子，roughness做安全下界；emissive factor/intensity各清晰；如果用户希望绝对替换材质，显式mode移除旧贴图或只选其一。

**验收**：同一白贴图调RGB可见；相同ORM粗糙度因子0.25/1差异；emissiveFactor0+emissiveTexture不发光；预览/导出按glTF材质factor一致。

#### R-18：贴图颜色空间控制未进入渲染合同

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/material_model.py:627`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/model3d_node.py:970`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/material_texture_loader.py:180`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6518`、`:6634`。

**前提/症状**：材质面板slot可选sRGB/linear，但bridge只保留path/role/uvset，不保留slot.color_space；loader保持原值，PBR全部color slot pow2.2。线性EXR或已IDT的ImageInput接base_color_in也只传glo丢TextureResult.color_space，shader再pow2.2，发生二次gamma。sRGB目前也只有2.2近似，低亮度不精确。

**解决**：TextureDescriptor保留ColorSpaceDescriptor；ModelPayload override保留颜色/alpha/data语义，不仅intglo；load或shader只做一次正确IDT，采用项目provider/等效GLSL IEC piecewise，data slot永不gamma；slot UV/color字段进cachekey。旧工程迁移默认和当前外观有明确兼容策略。

**验收**：相同材质使用srgb8bit与linear EXR等效；0.5linear连接override仍0.5而不是0.2176；面板选择linear与sRGB有可预期差异；normal/ORM不改数据；HDR>1不clamp。

#### R-19：独立metallic与roughness输入相互覆盖

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/scene3d_node.py:1060`、`:1129`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6526`。

**前提/症状**：Model3D同时连接metallic_in与roughness_in。两者映射slot="orm"，循环后者覆盖同textureunit。PBR从这最后一张图同时读G粗糙、B金属，也可能从R误作为AO；独立灰图无法独立控制两个属性。

**解决**：分别sampler与hasflag，并定义单通道读取；或GPU显式生成ORM（AO R、roughness G、metallic B），保留来源/通道schema；只有实际AO连接/声明ORM有O时采AO，不把未知R当遮蔽。

**验收**：metallic黑/roughness白=粗糙非金属；互换=光滑金属；单独接任一时另一scalar保留；AO默认1；连接次序不影响结果。

#### R-20：材质cache key缺shader program身份

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/scene3d_node.py:4560`、`:4641`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/material_state.py:36`。

**前提/症状**：同MaterialData被非蒙皮/蒙皮或instanced等不同program的mesh连续使用，has_tangent相同。key只material/texture/params/has_tangent，program换了仍matches；uniform是program-local，第二program的sampler位置/has_map/intensity未正确设置，可能沿用0或历史材质。已修has_tangent项不能覆盖program差异。未用真实混合模型做视觉复现，因此证据B、建议先focused验证再改。

**解决**：cache区分GL texturebind与programuniform两层，或key含programglo；program切换至少初始化所有材质uniform（texturebind可复用）。重置/上下文重建清samplerprogram集合。

**验收**：同材质在skinned/non-skinned/instanced连续渲染与关闭cache参考像素一致；有贴图、无贴图、normal/emissive变体全测；统计cache收益不能换来输出差异。

#### R-21：体积文件热更新命中内部旧内容

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/volume3d_node.py:195`、`:242`、`:246`；runtime_file_signature`:151`。

**前提/症状**：播放头不变，同path的.npz密度文件被覆盖。外部runtime_file_signature变了会促使重求值，但内部content_sig不含mtime/size，只path/frame/params；有旧GPUtexture则提前return、load_volume不运行。真实节点算法+fake上传小复现：density1→2，file signature确实变；upload_count仍1、alpha仍0.6321206，正确0.8646647。

**解决**：先解析序列路径，os.stat纳秒mtime/size进内部content_sig（缺文件/出错也invalid）；避免int秒mtime遗漏同秒修改；来源缓存和节点内缓存同维度。

**验收**：静态.npz原地覆盖/同秒同size变化可刷新；sequence当前帧文件更新可刷新；文件删除不输出旧图；不修改时同纹理重用。

#### R-22：动态procedural与表达式图被静态导出

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:56`、`:72`、`:198`；入口 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/app/controllers/composite_controller.py:991`；dynamic capability `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/noise_node.py:78`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/particle_node.py:362`。

**前提/症状**：独立合成noise.animate=True、particle，或transform表达式translate_x="frame"，没有video_frame/keyframe。真实detect_composite_mode三个输入都IMAGE，resolve_range=(0,0,24)，页面Dialog按IMAGE只提供单图。节点本身支持按frame动画，但渲染输出无法选择范围。

**解决**：调用统一frame-dependency分析/节点capability，不复制少数类型清单；检查expressions引用frame/time、序列文件pattern、particle、model自动动画；UI允许用户手动选择静态单帧/范围，duration无metadata时仍可输入范围；range来源包括表达式/工程工作区，不能假造动画长度。

**验收**：noise.animate、particle、frame/time表达式、volume/image序列、model动画能选择视频与序列；非time表达式不被误判；用户可手动导出一帧或范围；默认range合理且可编辑。

#### R-23：数据AOV使用显示格式时被无提示裁切

**位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_render_output.py:1392`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/shaders.py:6259`、`:6276`；格式选择 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_render_dialog.py:399`。

**前提/症状**：position、motion_vector等含signed或>1数值的pass选择PNG/TIFF。当前已有非beauty保护，**不会**施加sRGB/PQ OETF，这个旧风险不要重复记为bug；但dtype转换仍直接clip(0,1)。真实helper输入[-2,0.5,3]，PNG输出[0,127,255]，TIFF[0,32767,65535]；EXR保留[-2,0.5,3]。negative motion、世界坐标不能恢复。normal当前编码normal*0.5+0.5，可存0..1；depth为规范化深度，也要明确linear/near/far元数据，不能混淆世界距离。

**解决**：每个AOV声明semantic、范围、通道数、精度、可逆编码；position/motion/deep默认要求float EXR，并在preflight禁用不支持格式；如用户只想可视化PNG，显式选择visualization mode（附比例/偏移），不能作为原始pass。4通道/coverage与alpha读回问题一并按R15升级，输出保持精确数据。

**验收**：负向/正向运动vector及大于1、负worldposition在EXR中逐值保留；PNG原始数据模式被明确阻止或可逆metadata足够恢复；normal/depth不额外OETF；AOV导出不按beauty色彩偏好改值。

### 能力不足与建议优化（不直接归为已证 bug）

| 编号 | 不足及证据 | 当前可复用路径 | 解决建议 | 验收 |
|---|---|---|---|---|
| RG-01 | 合成Render Output无音频开关、source audio/mix接线；render_video没有add_audio_stream/write_audio_samples调用 | shared FFmpegVideoEncoder已有audio API；Edit时间线已有混音/区间语义 | 输入有audio先提示输出静音；添加保留输入/时间线混音选项，经既有AudioMixer按范围裁切重采样写入；retime需要明确音频stretch/offset合同 | 含音频测试视频输出audio stream，音画同步，非零start和retime/cancel各验 |
| RG-02 | 图片序列只有format，settings.options={}；16-bit PNG、EXR压缩/half/full、channels、alpha选项不可达 | FileOutput节点已经有format/depth/channels/alpha_mode和写入选项 | 抽公共输出schema和转换服务，页面对话框调用，避免第三套转换 | 16bit PNG实际uint16、EXRhalf/full可读取、unsupported组合被预检 |
| RG-03 | Volume3D为CPU固定前向投影的.npz吸收；.vdb显式依赖拒绝；pointcloud CPU2D正交点图，没有接Scene3D几何/相机 | volume_grid/point_cloud纯数据模型已有；相关节点DESCRIPTION已说明MVP | UI明确独立2D rasterizer能力；若要3D交互，ScenePayload+GL_POINTS/volume pass，通过统一camera/occlusion；OpenVDB先做跨平台packaging gate | 相机旋转/深度遮挡有效；volume预算内，未依赖时明确错误 |
| RG-04 | 材质高级字段transmission/clearcoat/sheen/subsurface/normal_strength/UVTransform有model序列化，但当前PBR无完整消费；面板玻璃preset只emit4个scalar | material_model版本与override已支持存储 | 首先声明shader能力，未支持项禁用并显示原因；玻璃preset在R16实现前不宣称完整玻璃；按feature增shader/AOV/export parity | 各可用控件产生可证像素变化；不支持项有能力标识 |
| RG-05 | pool有acquire_tiered_texture(export→f4)，大部分effect还走legacy.acquire_texture默认f2；linear_f32读回≠全链32bit计算 | GPUResourcePool tiering API已有 | 在RenderPlan附精度需求，HDR数据AOV/最终EXR按需f4，预览保f2；明确定义precision选项/预算；先测再迁移，避免盲目翻倍显存 | 数值gradients与CPU参考误差达标，4K内存可控、HDR峰值不溢出 |
| RG-06 | RenderExport blocking wait可到120s；shared context route仍同步等待，是否UI线程取决于controller入口 | CompositeRenderer command queue+done_event已有 | 复核所有调用线程；图片导出以QObject signal/future完成，UI只接进度与结果；对超时取消排队任务而不是让后台继续做已遗弃结果 | 4K图片导出期间UI可拖动/取消，timeout任务可退场，结果不会覆盖后续请求 |
| RG-07 | 预检对cycle/output/missing media有防线，但codec/transfer/depth/alpha/音频与模型能力缺少同一可视报告 | graph_preflight.py及node diagnostic badge已有 | 在现有report补目标格式能力诊断；按真实选项组合而非全局泛化警告；dynamic运行失败经worker传回 | 一个preflight可定位节点与不支持输出组合，并给可执行替代选项 |
| RG-08 | Cryptomatte目前只构建manifest，metadata.status=manifest_only；object_id仍byte编码pick ID，未实现标准多层hash/coverage EXR writer | render3d/cryptomatte.py已有hash/manifest纯数据合同，Scene3D可输出object_id | UI明确“ID matte / manifest”与完整Cryptomatte交付的能力差别；实施float32 hash/coverage排名层与EXR metadata writer，避免half精度/三通道丢失 | 外部Nuke/Blender读取标准layer/manifest，抗锯齿边缘coverage正确，未知名称稳定 |

RG-06 是线程/响应性能复验项，本文没有在完整UI线程证明卡顿，不能直接作为已确认产品bug。context实际native对象释放也需独立长测：ExportRenderer目前ctx=None而不显式release，私有standalone context与wrapped共享context应分ownership，不能用同一cleanup处理；不凭源码单独断言泄漏。

### 覆盖与尚无法验证

已检查主调用链：UI CompositeController → CompositeRenderDialog → CompositeRenderWorker → CompositeRenderOutput → CompositeFrameRenderer → GraphSnapshot → CompositeExportRenderer / GraphEvaluator → GPUResourcePool → readback → shared FileIO / FFmpegVideoEncoder；同时检查preview CompositeRenderer eval context、MotionBlur上下文、Paint/AI上传、3D Model/Scene/PBR/material loader/caches/volume/pointcloud。

已验证的失败/取消路径包括：序列中途取消、某帧None、并行context factory异常、Paint/AI上传位宽、资源cleanup遗漏与体积source改动。真实GPUPool复现和CPU/PyAV实际转换区别于fake frame的业务状态注入，见证据口径。

尚待根测试或后续复验：真实glTF/FBX alpha/blend/mask视觉比较；共享材质跨program模型；所有3D AOV及cryptomatte外部DCC读取；真实audio多声道与retime；Windows/Linuxcompute shader（本机macOSGL4.1没有compute）；多GPU/VideoToolbox/nativeGPUlease所有故障；所有容器codec高位深与alpha组合；真实4K持续播放内存、取消重入、多窗口关闭时机；图内所有effect的像素参考金标准。现有单测通过不能替代这些验收。

建议根计划先合并R01/R02与R03–R07色彩合同，再修R08–R13交付完整性/生命周期；R-14–R19解决3D与透明输出一致性；R-20–R22缓存和动画识别；最后按实测4K数据优化N项。本文不复述旧审计报告，只保留当前源码与本次运行可证内容。
## 证据与重跑入口

- evidence/suite_results.json、pytest_logs/*.log/*.xml：逐文件原始退出与用例结果。
- evidence/exr_env_rerun.log/.xml：EXR前提复测。
- evidence/media_probe.json、environment.json、node_inventory.json：素材/环境/节点底数。
- evidence/media_bench/*.json/*.log：每帧耗时、实际decoded_frame、像素shape/dtype/hash、frame trace、diagnostics。
- evidence/focused_repro/：各项独立复现脚本和输出；evidence/source_notes/：三路原始子报告。
- evidence/run_isolated_suite.py、run_media_benchmark.py、run_real_app_benchmark.py：审计重跑入口；从当前项目根目录用.venv/bin/python执行，性能基准与套件顺序运行以减少争用。

基准使用临时GL/测试工程；不写原视频，不应把输出路径中的fake文件误当交付视频。本次pytest改写了已跟踪的p29基准JSON，审计输出另存evidence后已恢复原文件，最终产品与原测试数据无变更。
