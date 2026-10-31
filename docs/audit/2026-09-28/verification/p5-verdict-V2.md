# P5 抽查复核结论 V2（16 条）

复核人：P5 抽查复核子智能体（独立 Read 源码验证，未采信报告原文）
日期：2026-09-28
任务书：docs/audit/2026-09-28/verification/p5-sample-V2.md

总计：成立 16 / 部分成立 0 / 不成立 0。无降级建议。

## [AUD-19740] fps 下拉 "Custom" 项触发 float("__custom__") ValueError 崩溃
- 结论：成立（维持 Critical）
- 关键验证：comp_clip_settings_dialog.py:65 `_CUSTOM_KEY = "__custom__"`；:441 `float(self._fps_combo.currentData() or 24.0)`——"__custom__" 为 truthy 字符串，float() 必抛 ValueError。:268-270 Custom 为普通可选项、fps 行（:259-271）确无输入控件；:397-407 `_sync_fps_combo` 对非预设 fps（预设恰为 8 项，无 12/15/48/120）自动落 Custom。调用方 edit_controller.py:5192-5228 `_on_finished` 槽只有 try/finally 无 except，5197 调 get_settings()，异常逃出 Qt 槽触发 PyQt 默认 qFatal。
- 出入：无

## [AUD-31703] 图像尺寸静默强制 2048×2048，aspect_ratio 被忽略
- 结论：成立（维持 Major）
- 关键验证：volcengine_generation_provider.py:288-303 `_body`：`if width * height < 3_686_400: width = height = 2048`（3,686,400 = 1920×1920，1280×720/1080×1920 均命中）；全文件 grep 确认 aspect_ratio 零使用（仅 recipe.width/height）。generation_adapter.py:85-99 harness 主路径构造请求时不传 recipe（默认 None）但携带 aspect_ratio（generation_models.py:252 字段存在）。
- 出入：无（报告引用 generation_adapter.py:85-105，实际构造体在 85-99，行号微偏不影响结论）

## [AUD-26704] insert_keyframe 同帧替换后 undo 丢失原关键帧
- 结论：成立（维持 Major）
- 关键验证：command_catalog.py:752-753 apply 记录 `existing` 到 `_existed`；apply 时 :756-762 新 Keyframe 经 add_keyframe 替换原帧（keyframe.py:142-155 add_keyframe 同帧原地替换语义，docstring 自述）。undo :775-780 遍历仅跳过新 keyframe（`kf is not existed` 恒真——existed 已不在替换后的 track 中），从不回填 `_existed`，该帧关键帧被整体删除。
- 出入：无

## [AUD-25903] 曲线两点路径 clip(0,1) 与 PCHIP/Widget 线性外推不一致
- 结论：成立（维持 Major）
- 关键验证：scripting/api.py:119-122 两点路径 `return np.clip(lut, 0.0, 1.0)`；同函数 PCHIP 路径 :133-153 仅有外推、`return res / 255.0` 无 clip；且 :104-105 docstring 自称 "matches CurvesWidget.calculate_curve behavior"。curves_widget.py:446-453（位于 src/shared/ui/widgets/）注释明确"使用线性外推处理超出范围的值，避免裁切色彩信息"。
- 出入：无（curves_widget 实际路径为 src/shared/ui/widgets/curves_widget.py，报告未写全路径，行号精确吻合）

## [AUD-15513] scrub 预览 worker 与播放启动竞态
- 结论：成立（维持 Major）
- 关键验证：audio_playback.py:4232 `preview_scrub_audio` 仅入队时检查 `is_playing`；`_scrub_preview_generation` 全文件仅在 :4242（preview 入队内）递增，play()（:4355-4392）不递增、不排空 worker 队列，仅 :4361 `_stop_scrub_preview_output` 停输出设备。worker :3625-3634 mix 前 reset_dsp_states() + mix_samples(frame)，:3609/:3648 仅查未变的 generation；:4269 `_on_scrub_preview_samples_ready` 在 emit 后才丢弃输出，破坏已发生。
- 出入：无

## [AUD-25814] set_params 绕过 ColorWarper 状态复位防护
- 结论：成立（维持 Major）
- 关键验证：warper_panel.py:98-105 手工复制重建（直赋 grid_size/points、循环调私有 `_initial_position`、apply_deltas），不调用 set_grid_size/reset_grid，不停 `_changed_timer`、不清 `_pending_deltas`。color_warper.py:68-77（src/shared/ui/widgets/）reset_grid 注释明确"must not later publish an obsolete drag sample"并 stop timer + 清 pending。tools_panel.py:232 图层 hydrate（undo/选择切换）调用 set_params，待发 16ms 定时器随后经 warper.changed 发布陈旧 deltas（blockSignals 已在 :111 解除）。
- 出入：无

## [AUD-16500] close/invalidate 三条关闭路径无视 P2-09 租约
- 结论：成立（维持 Critical）
- 关键验证：edit_gpu_decode_manager.py:186-190 租约契约注释自述"逐出只回收租约为 0 的会话，杜绝解码中途会话被关闭的 C 层 use-after-free"；租约检查仅存在于 LRU 逐出 :1348。close_source（:2878-2911）、close_all（:2913-2969）、invalidate_all_sources（:2971-3016）均在锁内清空 `_source_refs` 后锁外直接 `source.close()`，零租约校验。调用时序核实：timeline_preview_renderer.py:6443 set_preview_quality 运行中直接 invalidate_all_sources()；stop() 路径 :1283-1287 注释自认 "SIGSEGV（真实复现）"，:1295 `shutdown(wait=False, cancel_futures=True)` 后 :1301 立即 close_all()。
- 出入：无（任务书位置栏"2926-2969 同构"为节选；报告中 1295/1301 行属 timeline_preview_renderer.py，正文括注已可定位）

## [AUD-36813] with_modified/with_active_timeline 手工逐字段重建
- 结论：成立（维持 Suggestion）
- 关键验证：manifest.py:202-240 两方法逐字段构造 ToposManifest，清点各 15 个字段当前携带完整（含 edit_content_schema_version），无现存丢失；ToposManifest 为 `@dataclass(frozen=True)`（:42-43），`dataclasses.replace` 建议可行。属维护性风险（新增字段漏带 → 静默重置），Suggestion 恰当。
- 出入：无

## [AUD-0510] 粘贴复用同一 patch dict，目标间共享可变嵌套结构
- 结论：成立（维持 Major）
- 关键验证：grade_clipboard.py:197-203 broadcast 与取模复用同一 patch dict 对象，无 deepcopy。layer.py:731-736 apply_grade_patch 的 mask_shapes 分支走 `MaskShape.from_dict(d)`；from_dict（:200-239）对 points/grad_points/center/size/brush_strokes 直接 `data.get(...)` 存引用。对照同函数其他分支（mixer :702、effects :705、qualifier :712、curves :718、hsl :722、plugins :747）全部 deepcopy——mask_shapes 是唯一漏网分支。canvas.py:1822 `self.bezier_points[idx]['pos'] = pos`、resize 分支 `self.size[0] = ...` 就地编辑确认跨目标污染链路。
- 出入：无

## [AUD-13500] complete_host_export 引用未定义名 subtitle_duration_frames
- 结论：成立（维持 Critical）
- 关键验证：export_service.py:765 `brand_timeline_duration_frames=subtitle_duration_frames,`；参数表（:697-724）仅有 `subtitle_timeline_duration_frames`（:714）与 `brand_timeline_duration_frames`（:720）；全文件 grep `subtitle_duration_frames` 仅命中 :765 使用处——非参数、非局部、非模块级，每次调用求值关键字实参必抛 NameError。:763-767 续行多缩 8 格的拼接残迹肉眼可辨。调用方 ai_edit_host_bridge_impl.py:1239 的同名局部变量在 export_service 命名空间不可见；:1259 于 `_post_export` 编码 worker 回调中调用，导出完成链路（probe/质量门/终态）整体断裂。
- 出入：无

## [AUD-20903] CPU 回退为 UI 线程同步解码
- 结论：成立（维持 Major）
- 关键验证：source_viewer.py:2161-2169 `_load_video_frame` 在 UI 线程同步 `read_frame(frame)`，:2159 trace 自注 `sync_seek_count = 1 # read_frame seeks keyframe + decodes forward`；:2220-2233 `_on_gpu_timeout` watchdog（QTimer，UI 线程）直接 `_cpu_fallback_decode`；:2235-2253 `request_cpu_fallback` docstring 自述 "runs on the UI thread because it uploads a QImage texture"，:2253 → :2255-2261 同步 read_frame。三条路径全部 UI 线程阻塞事实确凿。
- 出入：无

## [AUD-8701] Stretch 变形可产生 NaN/inf 顶点上传 GPU
- 结论：成立（维持 Critical）
- 关键验证：cpu.py:246-249 `stretch_f = 1.0 + (amount - 1.0) * weights; squash_f = 1.0 / np.sqrt(stretch_f)`；:68 weights 含 strength（[0, strength]）。deformer3d_node.py:66 strength max=5.0、:73 amount min=-5.0。报告示例复核：amount=0.5/strength=2.5 → stretch_f = 1-0.5*2.5 = -0.25 → sqrt 负数得 NaN（仅 RuntimeWarning）。触发域实际更宽：amount=0/strength=1（UI 默认强度）即 stretch_f=0 → squash_f=inf。except 仅捕 ValueError/TypeError/LinAlgError（:306/:320），NaN/inf 不抛异常直入 MeshData → VBO。
- 出入：无（触发条件比报告示例更宽松，方向一致）

## [AUD-18900] _GuardedPreviewSource acquire/retire 无锁 check-then-act
- 结论：成立（维持 Critical）
- 关键验证：timeline_preview_renderer.py:447-470 acquire（closed 检查与 inflight+=1 之间）、retire（closed=True 与 inflight<=0 判定）均无锁（`__slots__` 无 lock 成员）；docstring :430-438 自述 "avcodec 段错误（8 轨场景 ~1/3 复现）"。调用点核实：worker 侧 acquire 在 `_direct_preview_lock` 之外（:4298，with 块 4279-4296 已结束）；UI 侧 `_clear_direct_preview_sources`（:4028-4044）在锁外逐个 retire——interleaving 窗口真实存在，retire 在时间线重建/视口变化高频发生。
- 出入：无

## [AUD-27900] HTML 场景 svg/html_sequence 导出无 pixel_format 分流
- 结论：成立（维持 Critical）
- 关键验证：export_service.py:213-214 按 format_name 直接分流 `_write_sequence`，无 backend/pixel_format 校验；:497-502 svg_sequence `write_bytes(frame.data)`、html_sequence `frame.data.decode("utf-8")`。scene_renderer_router.py:188 注释 "RendererResult.frame is already a RenderedFrame (pixel_format rgba8)"、:317-321 DOM overlay 适配器透传 pixel_format；svg_renderer.py:572 `pixel_format="svg"` 且 data 为 UTF-8 编码文本。ExportOptions 默认 format="svg_sequence"（export_service.py:25）。HTML 场景 + 默认格式 → RGBA 字节写 .svg 静默损坏；html_sequence → UnicodeDecodeError。
- 出入：无

## [AUD-5300] 共享内存模糊 padding 协作装载只填每 16 槽之一
- 结论：成立（维持 Critical）
- 关键验证：shaders.py:3789 `shared_idx = local_id.x + kernel_size`。H 左装载守卫 :3799 `local_id.x == (k - 1) % 16`：线程 t 仅当 k ≡ t+1 (mod 16) 时写槽 kernel+t-k，全线程合计只覆盖 {kernel-1, kernel-17, kernel-33, kernel-49}，而需覆盖 [0, kernel-1] 共 kernel 个槽；右装载守卫 :3818 `local_id.x == (16 - k) % 16` 同理仅覆盖 {kernel+16, +32, +48, +64}。卷积读取 :3841-3844 `s_row[shared_idx + i]`，i ∈ [-kernel, kernel] 命中大量未写槽（GLSL shared 无初始化，值未定义）。V pass :3895-3930/:3937-3942 同构（s_column，守卫用 local_id.y）。compute_shader_manager.py:454-456 `use_shared = radius > 32.0` 确认大半径强制走此路径（radius>32 → sigma>16 → kernel≥49）。
- 出入：无（守卫数学逐一推导复核，与报告的槽位集合结论一致）

## [AUD-8604] animation.py 公共 API 类型注解不完整
- 结论：成立（维持 Suggestion）
- 关键验证：animation.py:205 `local_matrices: dict`（裸 dict）；:238-242 `sample(...) -> dict`；:305 `def select_clip(clips, name: str):`（clips 无注解、无返回注解）；:325 `def clip_names(clips) -> list:`（裸 list）。事实全部核实，违反项目强类型约定，Suggestion 恰当。
- 出入：无
