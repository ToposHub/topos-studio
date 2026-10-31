# P5 抽查复核结论 V1

- 复核人：P5 抽查复核子智能体
- 日期：2026-09-28
- 任务书：docs/audit/2026-09-28/verification/p5-sample-V1.md（16 条）
- 方法：逐条先读来源批次报告完整论述，再独立 Read 源文件核对证据行号、触发条件与调用方可达性（grep 调用点）
- 汇总：成立 15 / 部分成立 1 / 不成立 0；建议降级 1 条（AUD-36100：Critical → Major）

---

## [AUD-36100] done-callback 异常逃逸泄漏信号量槽、run() 无超时死锁
- 结论：部分成立（建议降级为 Major；机制全部实证，现役触发路径缺失）
- 关键验证：d3d11_pipeline.py:107 except 仅列 (OSError, RuntimeError, TypeError, ValueError)；:118 `frame.release()` 裸调用（d3d11_texture_pool.py:113 重复归还抛 ValueError）；:192-205 槽位 `_slots.release()` 仅存在于 `_finish_success/_finish_failed`；:169 `future.result()` 无超时。`add_done_callback` 吞异常为标准库行为，机制成立。
- 出入：全仓 grep 确认 `D3D11PipelineScheduler` 生产代码零调用点（仅 windows_hw/__init__.py:74 导出 + tests/media/test_windows_hw_pipeline_policy.py 实例化），"导出线程永久死锁"当前无现役触发路径，属按导出管线 API 设计的潜伏缺陷，Critical 口径（用户可见崩溃/渲染错误）偏高。

## [AUD-38206] scene-linear RGBA 构造/刷新即量化为 8-bit sRGB
- 结论：成立
- 关键验证：color_picker_control.py:22-30 `linear_rgba_to_qcolor` clamp+int(round(…*255))；:73 构造、:118 set_rgba 均经此量化；:112-114 `rgba()` 回读走 QColor 往返，>1.0 分量必丢失。
- 出入：无（Suggestion 与"量化路径确定、HDR 调用方未证实"的自评一致）。

## [AUD-2710] 占位片段 fallback 轨道解析可抛 KeyError/StopIteration；未刷新媒体池
- 结论：成立
- 关键验证：edit_controller.py:8493-8496 逐字相符——守卫仅判 `new_project.timelines` 真值，`active_timeline_id` 缺失抛 KeyError、`video_tracks` 空抛 StopIteration；:8483 写回 `_current_edit_project` 后至函数结束（:8514）无 `media_pool_panel.set_project`（其他路径 :362/:630/:694/:878 均有）。
- 出入：无。

## [AUD-20600] closeEvent 对 wait 超时后仍在运行的预览线程 deleteLater
- 结论：成立
- 关键验证：scene_cut_dialog.py:2663-2667 `wait(1000)` 返回值被忽略后无条件调 `_on_preview_worker_finished`；:1993 无条件 `worker.deleteLater()`；PreviewLoadWorker.run 仅 :1376/:1384 在解码前后查中断，:1379/:1383 解码本身不可中断；:1344 docstring 自认长 GOP/4K/网络媒体场景。
- 出入：无（销毁运行中 QThread 属 Qt 明示 UB，Critical 恰当）。

## [AUD-23108] fetch_result SUCCESS 路径访问 `self._svc._jobs` 私有属性
- 结论：成立
- 关键验证：composite_adapter.py:496-498 逐字相符（含 `# type: ignore[attr-defined]` 自证契约破裂）；:456-458 duck-type 注释与模块 docstring :13-14 "生产环境替换为真实 service，接口一致" 的承诺冲突；`_jobs` 仅为 Fake 服务内部字典（:348 等）。当前唯一使用方为测试（test_p6_acceptance.py），属模块自述契约下的结构性破坏。
- 出入：无（Major 恰当）。

## [AUD-32500] to_ocio_name 未映射组合静默回退 "lin"，OCIO 变换被整体跳过
- 结论：成立
- 关键验证：color_space_descriptor.py:81-89 LINEAR 分支非 AP1 一律 "lin"、:89 `_OCIO_NAME_MAP.get(…, OCIO_LINEAR_NAME)` 静默回退；:194-210 映射表确缺 (bt2020,bt709)/(bt2020,srgb)/(smpte432,smpte2084)/(bt709,pq) 等 UI 可选组合（:374-395 available_primaries/transfers 提供这些选项）；color_transform_provider.py:696-698 `src_ocio == dst_ocio` 时整体跳过 OCIO——线性数据被当已编码信号导出，无警告。
- 出入：无（用户可选配置下的静默渲染/色彩错误，Critical 恰当）。

## [AUD-6316] 输出 TextureResult 沿用输入 bbox/format，实际尺寸不一致时元数据失配
- 结论：成立
- 关键验证：ai_gen_expand_node.py:152-155 `output_width/height` 参数优先（:91-93 ParamDef 确为可序列化参数）；:236-237 `w, h` 取自 `rgba.shape`，:239-245 TextureResult 却沿用 `image.bbox/image.format`——输出尺寸 ≠ 输入时声明数据窗口与纹理不符。
- 出入：无。

## [AUD-22414] Supervisor 记账字典只增不删，长会话无界内存增长
- 结论：成立
- 关键验证：subagent_runtime.py:131-134 三个 dict 定义；:176-178（run）、:221-223（continue_child）写入；全文件对这三个 dict 无任何 pop/del/clear（唯一 pop 在 :114-115 的 registry `_definitions`）；`_requests` 含完整 prompt 字符串，Supervisor 经 composition_plugin.py:224-228 长期持有。
- 出入：无。

## [AUD-32606] DEFAULT_CONFIG 全程浅拷贝，嵌套容器与类属性共享
- 结论：成立
- 关键验证：config_manager.py:367 `{**DEFAULT_CONFIG, **loaded_config}`、:388 与 :618 `DEFAULT_CONFIG.copy()`、:634 `{**DEFAULT_CONFIG, **disk_config}` 全为浅合并/浅拷贝；:13-49 DEFAULT_CONFIG 含 `page_layouts/recent_files/features` 等嵌套容器，用户配置缺键时实例直接别名类属性。
- 出入：无（报告已如实标注当前调用方先复制、暂未实际污染，Suggestion 恰当）。

## [AUD-22322] brief_template/input_schema 数值裸 int()/float()，畸形 manifest 炸掉目录渲染
- 结论：成立
- 关键验证：skill_catalog.py:74-76 `int(td_default["ideal"])` 等三处裸转换逐字相符；:123-124 `float(spec["minimum"])` 同样裸转换；cards()（:396-401）→ _build_card → :437 `build_form_fields(m)` 无任何 try/except，单个畸形 manifest 即中断整个目录。外部数据源存在：plugin_persistence.py:601 从持久化 dict 取 `input_schema`。
- 出入：无（Major 恰当，外部数据未验证铁律）。

## [AUD-36816] check_offline_assets 未知类型静默空报告，与 relink/batch_relink 报错不一致
- 结论：成立
- 关键验证：offline_asset_manager.py:248-249 `else: total_assets = 0` 后照常构造 offline_count=0 报告；对照 :353-354 与 :401-402 relink_asset/batch_relink 同分支均 `raise ValueError(f"Unsupported project type: …")`，不对称确证。
- 出入：无。

## [AUD-32203] ToposMuxFile 顺序写回调未持实例锁，与 seek_write 加锁不对称
- 结论：成立
- 关键验证：topos_binding.py:2488-2491 `_write_cb` 的 `os.write(self._fd, chunk)` 无锁；:2495-2501 `_seek_write_cb` 传 `self._wlock`；:2498-2499 注释自述"与顺序写共用实例锁互斥"但顺序写侧从未 acquire；:166-180 `_pwrite_full` Windows 模拟为 lseek→write→恢复指针，共享 fd 偏移可被无锁 os.write 插入。
- 出入：无（报告已如实披露 native 侧是否并发调用回调未确证，锁不对称事实成立，Major 维持）。

## [AUD-18500] deep-u16 效果桥代码块缩进脱出 `if deep_u16:`
- 结论：成立
- 关键验证：timeline_gpu_export_renderer.py:1593 `if deep_u16:` 前导 12 空格、块体 16；:1634-1641 前导 12——确在块外；:1617-1618 no-grade 早退使 deep_u16+效果链被静默丢弃（症状 A）；:910 `_tex_mid_u16=(None,None)`、`_ensure_u16_fbo_pair` 仅 :1604（deep 分支）调用，deep_u16=False 时 :1634 取到 None → :1356 `src_tex.use(0)` 抛 AttributeError 被 :1361-1362 吞掉返回 None → 整帧回落 CPU（症状 B）；调用方可达（timeline_export.py:7984 render_layer_stack / :8143 _gpu_opengl_effect_chain）。
- 出入：无（静默渲染错误+主路径性能回退，Critical 恰当）。

## [AUD-6111] STANDARD_NAMES 漏登 4 个已注册 shader，Roto/MaskMerge/运动模糊恒降级
- 结论：成立
- 关键验证：shader_library.py:91-121 STANDARD_NAMES 无 roto/mask_merge/motion_blur_accum/mesh_pbr，而 _SHADER_SOURCES :181/:183/:186/:206 均注册；全仓唯一填充点 base_evaluator.py:3435-3438 遍历 STANDARD_NAMES；消费方 filter_evaluator.py:715（roto→None 即直通返回）、:1169（mask_merge→None 即返回 A）、base_evaluator.py:1863（motion_blur_accum→None 即 return None）恒走降级。
- 出入：无（静默渲染错误，Critical 恰当）。

## [AUD-10400] 使用未定义的 logger，粘贴 Topos Color 节点抛 NameError
- 结论：成立
- 关键验证：node_editor_actions.py:25-29 顶部仅 typing/uuid/copy，无 `import logging` 亦无 `logger =` 赋值；:796 `logger.info(` 为全文件唯一出现；:750（type=="topos_color"）+ :774（target_clip_id 非空）分支必经；node_editor_widget.py:2021/:3593-3596 确认 paste_nodes 为 Ctrl+V 主路径。
- 出入：无。

## [AUD-25600] set_params/reset 时 invert toggled 绕过状态锁强制启用限定器
- 结论：成立
- 关键验证：qualifier_panel.py:167 `invert_check.toggled.connect(self.on_param_changed)`；:454-458 on_param_changed 无 `_updating_flag` 防护且强制 `enabled=True` 后 emit；:706-741 `blockSignals(True)` 仅作用于面板自身、拦不住子控件，:710 `setChecked` 状态翻转时同步触发 toggled，:742 末尾再无条件 emit；下游链核实：main_window.py:1592 → image_engine.py:1418-1421 写 layer.qualifier_params，layer_controller.py:63 切层回填、delivery_matrix 检查 qualifier enabled。
- 出入：无（状态污染+渲染错误+持久化，Critical 恰当；触发条件invert 状态翻转）报告已正确限定。
