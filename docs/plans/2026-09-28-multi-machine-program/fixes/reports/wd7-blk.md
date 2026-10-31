# wd7-blk 修复报告 — 批 D 白名单解锁 9 条（g34/g35 两波 BLOCKED 遗留）

- 日期：2026-09-29
- 范围：REMEDIATION_PLAN.md 批 D 中前 7 条（g34/g35 波 BLOCKED：AUD-6710/6711、AUD-6800/6801、AUD-6804、AUD-6809、AUD-6900）+ 后 2 条跨文件条目（AUD-7101 求值器半边、AUD-7300）
- 白名单文件：`src/features/composite/nodes/{file_output_node,frame_interpolate_node,geo3d_node,group_node,image_input_node,scene3d_node}.py`、`src/features/composite/evaluators/base_evaluator.py`；新建 `tests/composite/test_fix_wd7blk_aud.py`
- 结果：**9 FIXED / 0 INVALID / 0 BLOCKED**
- 未改 REMEDIATION_PLAN.md；未做任何 git 分支操作/commit/push。
- 开工时 7 个白名单文件均为干净基线（git status 核实）；工作区其余未提交改动（video_frame/volume3d/warp/render_scheduler/composite_page/render3d/* 等，并行 agent 在途）本批未触碰，见"邻接失败归属"。

## 逐条结论

| ID | 结论 | 修复摘要 |
|----|------|---------|
| AUD-6710 | FIXED | `_prepare_for_export` 的 clip 条件由 `fmt != "EXR"` 改为**按目标编码是否浮点**判定：`float_output = (fmt == "EXR") or (bit_depth == "32")`，仅整数编码（8/16-bit）clip 到 [0,1]。TIFF+32bit 浮点路径（`astype(np.float32)`）不再截断 HDR 高光，与自身注释"Only clamp for non-float formats"的语义对齐；PNG/TIFF 8/16-bit 归一化行为不变。配合 AUD-6711 的前置验证，`bit_depth=="32"` 到达此处的只可能是 TIFF/EXR（PNG/JPEG+32 已被拒）。 |
| AUD-6711 | FIXED | 新增 `_BIT_DEPTHS` 组合表 + `_validate_export_settings(fmt, bit_depth, channels)` 类方法，`_do_evaluate` 在 **GPU readback 之前**前置校验：JPEG 仅 8-bit 且不支持 RGBA（OpenCV JPEG 编码器只收 1/3 通道）、PNG 仅 8/16、TIFF 8/16/32、EXR 任意（写路径恒 float32，参数只声明精度）——必然失败的组合不再走到 cv2.imwrite 才炸，直接进 `last_write_message`（含原因）。同时 `_save_to_disk` 经 `_write()` 包装记录 imwrite 返回 False 的编码器侧原因（格式/dtype/shape），异常路径记录 `str(e)`，`_do_evaluate` 把 `(_last_save_error)` 拼进失败消息——"原因被吞"修复。 |
| AUD-6800 | FIXED | 输出纹理改为**节点持有的单一持久纹理**（particle/constant 同款生命周期模型）：新增 `_ensure_output_texture(gl, w, h, resource_pool)`——优先复用现有纹理（同 ctx+尺寸零分配）→ `resource_pool.acquire_texture`（池化，归还走 `pool.release_texture`）→ 自建 `gl.texture`（自有，显式 release）。GPU blend 回退与 RIFE `_upload` 两条输出路径都渲染/写入这张纹理（`tex.write`），仅在尺寸或 GL ctx 变化时延迟释放旧纹理再重建。原实现两处（无池回退 + 上传结果）每次求值裸 `gl.texture()` 且 `own_tex` 标志无人消费——frame_interpolate 的 capability `owns_color_texture=False` → 不在 CACHE_OWNED_TYPES → CacheManager 也不释放，纯泄漏；现在 cleanup()/尺寸变化/换池均有明确回收路径，texture_id 跨帧稳定也消除了逐帧缓存条目悬垂风险。 |
| AUD-6801 | FIXED | 新增 `cleanup()`（evaluator 在节点移除时调用，base_evaluator.py:335 既有协议）：释放持久输出纹理（池化→归还池，自有→release）、blend program + quad VBO（try/except Exception + debug 日志，ctx 已死时不炸）、置空 `_interpolator` 引用（模块级 ONNX session 缓存有意保留——跨节点去重的共享缓存，非泄漏）。`_ensure_blend_resources` 的 ctx 切换分支同步修复：旧 ctx 的 program/VBO 原先被静默覆盖泄漏，现按 AUD-6607 同款"死 ctx drop-without-release"策略先弃引用再在 新 ctx 重建。 |
| AUD-6804 | FIXED | `_geo_revision` 递增改为**内容签名门控**：`_do_evaluate` 构建覆盖全部渲染相关参数（primitive/mesh_scale/instance 三参数/transform 6 分量/材质 5 分量/render_role+matte_id+shadow 2 分量）的 `content_sig` 元组，仅当签名变化时 `+= 1`（`__init__` 新增 `_geo_content_sig` 字段）。原实现每次求值无条件 +1 且作 `content_revision` 进 GeometryPayload.signature() → Scene3D 内容签名逐帧变化、静止场景恒 miss 缓存每帧全量重渲染；现在同参数重复求值修订号稳定（签名/缓存命中），任一参数变化（含动画逐帧）仍精确递增。`cleanup()` 顺带清空签名——cleanup 后再求值（VAO 重建为新实例）修订号递增一次。 |
| AUD-6809 | FIXED | `GroupNode.from_dict` 三处裸解构全部改走结构验证：新增 `_parse_port_mappings(raw, field)`（表非 dict → 空表+warning；条目非 `[member_id, port_name]` 字符串对 → 丢弃该条+warning，`v[0]` 对标量/None/短元组的 IndexError/TypeError 路径全部关闭）与 `_parse_member_ids(raw)`（非 list/tuple → 空表+warning；非字符串元素逐个丢弃+warning）。畸形工程文件（手编/异构 schema）不再在加载期崩溃，降级为干净空组并留定位日志；合法 roundtrip（to_dict→from_dict）逐字段不变；`is_expanded`/`group_color` 顺带容错强转。 |
| AUD-6900 | FIXED | legacy 同步路径（`_do_evaluate` 尾部）在调用 `upload_to_gpu` 前先判**解码相关输入是否变化**：`content_unchanged = 有活纹理 && UPLOADED_GPU && 路径存在 && !needs_file_mtime_check(解析后路径)`——`needs_file_mtime_check` 是既有 A6 契约（EXR part/extra 通道漂移 + 文件 mtime），命中则仅清 `_dirty` 保留现有 GPU 纹理，渲染线程零磁盘 I/O；否则照旧同步重载。任意参数编辑（premultiplied/frame_padding 等不碰像素的编辑）不再触发渲染线程同步 `decode_image_file`。失败态（FAILED）与文件缺失保持原重试/missing_policy 路径（保守边界，见下）；`reload()`/EXR 切片/mtime 变化照旧强制重载。缓存正确性不受影响：求值器缓存键含全量参数 hash（cache_manager.compute_key），参数编辑仍产生新缓存条目并拿到按当前参数构建的 TextureResult。 |
| AUD-7101 | FIXED | **求值器半边补齐**（g35 节点侧前置的根因残留）：`_evaluate_node` 的 effect 分支（原 base_evaluator.py:1746）由裸 `self.ctx` 改为与 source 分支同款 `ctx_arg = self._eval_ctx or self.ctx`——`CompositeEvalContext.frame_number` 由 `_evaluate_snapshot` 每帧写入（base_evaluator.py:553），noise 的 `_resolve_time`（getattr 安全）经此拿到播放头，`animate`/`animation_speed` → `time` → 分发端 `u_time` 全链路生效。安全性已全量勘察：65 个 effect 分类节点文件的 `_do_evaluate`/全文件**无任何 `ctx.<GL 方法>` 调用**（仅 4 个 source/output 文件直用 GL 且各自已 unwrap），effect 节点均为 marker 打包器，CompositeEvalContext（无属性代理的 dataclass）传入零风险；`_eval_ctx` 未设置时回落裸 ctx（测试环境/旧路径完全向后兼容）。缓存半边（animate=True → 帧号进缓存键）由 g35 的 `params_require_frame_cache` 覆写 + `_node_needs_frame_cache` C3 通道既有生效——两半拼齐，条目闭环。 |
| AUD-7300 | FIXED | 正交视图 depth renderbuffer 全生命周期记账（仿 `_aov_depth_rbs` 模式）：`__init__` 新增 `_ortho_depth_rbs`（top/right/front 三键）；`_render_ortho_views` 懒建分支创建的 `depth_rb`（原局部变量、无任何持有者）写入该表，且懒建条件扩为 tex/fbo/rb 三者任缺即重建（重建前先释放幸存的部分状态，防孤儿）；`_release_ortho_views` 在 tex→fbo 之后同步释放并置空 rb（FBO 先 RB 后，避免 attachment 悬挂；"深度缓冲随纹理释放"硬约束）。quad 关闭路径（5056）与 `cleanup()`（既有 `_release_ortho_views()` 调用）随即都覆盖 rb——每次 quad 尺寸重建泄漏 3 个 GL renderbuffer 的路径关闭。 |

## 设计决策与残留说明

- **AUD-6800 的持久纹理 vs 逐帧纹理**：frame_interpolate 是 `params_require_frame_cache=True` 的逐帧缓存源——若按"下一求值释放上一帧纹理"的逐帧模型，被 evaluator 帧缓存持有的旧条目会悬垂（g35 AUD-6902 修的正是这类陈旧 glo 问题，需连带失效协议）。持久单纹理模型（particle/constant 同款）让 texture_id 跨帧稳定、缓存条目永不悬垂，且把逐帧分配/释放坍缩为仅在尺寸/ctx 变化时发生——同时满足 deferred-release/池化硬约束。池化路径的归还不经过 CacheManager（该类型 `owns_color_texture=False` 不在其释放集内），由节点自身在替换/cleanup 时 `pool.release_texture` 显式归还。
- **AUD-6801 的 ONNX session 边界**：`cleanup()` 只丢节点对 `RifeInterpolator` 的引用；模块级 `_SESSION_CACHE`（跨节点会话去重）有意保留——清空它会使同图其它 FrameInterpolate 节点/重加回的节点付整次模型重载，属共享缓存设计而非泄漏。
- **AUD-6900 的保守边界**：跳过重解码仅在 `_load_state == UPLOADED_GPU && 文件存在` 时生效。FAILED 态保留逐次重试（瞬态解码失败可自愈，代价是失败期间仍同步重试——与既有语义一致，未在本条扩大战线）；文件缺失仍走原 `upload_to_gpu → missing_policy` 路径（占位策略语义不变）。序列路径（####）的帧号变化仍由 AUD-6901 的 worker 通道接管，互不干扰。
- **AUD-7101 的传递范围**：只改 effect 分支。passthrough（1728，dot/noop/switch 帧无关）与 output 节点（1768，FileOutput 的 `_readback_texture` 以裸 ctx 直调 `ctx.texture`，传 eval_ctx 反会破坏其 GPU 读回）维持裸 ctx，属有意决策。若未来 FileOutput 需要帧号/配置上下文，其 `_readback_texture` 需先做 unwrap（本批不动）。
- **AUD-7300 与并行批的衔接**：scene3d_node.py 上有 AUD-7301/7302 批的未提交改动（`_id_write_buffers`/`_outline_buffers`/LOD VAO 记账），本批改动与其无交集（正交视图段），基于当前混合工作树实施且全部 scene3d 相关邻接测试通过（预存在失败除外，见下）。
- **锁定旧行为协议**：无需翻正任何既有测试——21 例失败均经 git stash 基线复核证明与本批无关（见下），未触碰。

## 验证记录

- `py_compile`（.venv/bin/python -m py_compile）：7 个改动源文件 + 新测试文件全部通过（PY_COMPILE OK ×8）。
- 新增回归：`tests/composite/test_fix_wd7blk_aud.py` — **49 passed**（9 条目全覆盖：TIFF32/EXR 保 HDR、TIFF16/PNG8 仍钳制；8 组合有效/5 组合拒绝/求值早退不触 readback/imwrite False 与异常原因入消息；池复用单次 acquire/尺寸变化归还池/自建回退释放/池失败回退/死 ctx drop-without-release/upload 持久纹理零新分配；cleanup 全释放/ctx 切换重建且不 release 死对象；修订号同参稳定/参数变化递增/cleanup 后递增/VAO 不重建；畸形映射 5 形态/表非 dict/短元组/member_ids 非法元素与非表/标量强转/合法 roundtrip；参数编辑零重解码/exr 切片重载/mtime 重载/缺文件与 FAILED 保留旧路径；effect 收 eval_ctx 帧 42/无 eval_ctx 裸回落/noise 端到端 time=7 与裸 ctx time=0；rb 创建入账 3 视图/释放三件套/尺寸重建旧 rb 全释放/cleanup 释放）。
- 邻接既有测试（最终复跑）：
  - 集合 A（frame_interpolate + group×2 + quad_view_quality + scene3d_view_content_split + scene_payload + 3d_phase1）：169 passed / 13 failed（**预存在**，见下）；
  - 集合 B（composite_cache + cache_key + cache_strategy + cache_frame_retention + graph_evaluator_consistency + pass_fusion + keyframe_cache_invalidation + node_extension_roundtrip）：**151 passed**；
  - 集合 C（wd6g34 + wd6g35 + scene3d_node_coverage + msaa_grid + motion_aov + quality_profile）：323 passed / 8 failed（**预存在**，见下）；
  - 集合 D（graph_preflight + nodes_registry + batch_export + parallel_export + export_provider_thread_route）：**68 passed**；
  - 集合 E（composite_node_contract + node_contract_classification + node_capabilities + wd6g36 + wd6g38）：**160 passed**；
  - 集合 F（video_frame_playback + time_range + video_frame_index + topos_color_contract + shader_uniform_contract + transform_motion_blur + cryptomatte_select）：**88 passed**；
  - 集合 G（composite_merge_semantics + p7_object_interaction + 新测试）：**145 passed**。
- **邻接失败归属（非本批引入）**：21 例失败（集合 A 13 例：quad_view_quality×4、scene3d_view_content_split×8、3d_phase1_correctness×1；集合 C 8 例：scene3d_msaa_grid×4、scene3d_quality_profile×4）全部为 **TestCompositePageNav3D/TestViewSignature/Test2DHideGrid 等 composite_page.py / render3d 域源码锁测试**——目标文件正被并行 agent 修改中（composite_page.py、camera_model.py 等在工作区未提交 diff 内，不在本批白名单、本批未触碰）。已用 git stash 将本批 7 个文件暂存后复跑：**同样 13+8 例失败、逐一相同**（基线复现），恢复后无变化——与本批改动无关。
